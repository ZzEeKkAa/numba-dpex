# SPDX-FileCopyrightText: 2023 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0

"""Provides a helper function to call a numba_dpex.kernel decorated function
from either CPython or a numba_dpex.dpjit decorated function.
"""

from typing import NamedTuple, Union

import dpctl
from llvmlite import ir as llvmir
from numba.core import cgutils, types
from numba.core.cpu import CPUContext
from numba.core.types.containers import UniTuple
from numba.core.types.functions import Dispatcher
from numba.extending import intrinsic

from numba_dpex import dpjit
from numba_dpex.core.targets.kernel_target import DpexKernelTargetContext
from numba_dpex.core.types import DpctlSyclEvent, NdRangeType, RangeType
from numba_dpex.core.utils import kernel_launcher as kl
from numba_dpex.dpctl_iface import libsyclinterface_bindings as sycl
from numba_dpex.dpctl_iface.wrappers import wrap_event_reference
from numba_dpex.experimental.kernel_dispatcher import KernelDispatcher


class LLRange(NamedTuple):
    """Analog of Range and NdRange but for the llvm ir values."""

    global_range_extents: list
    local_range_extents: list


def wrap_event_reference_tuple(ctx, builder, event1, event2):
    """Creates tuple datamodel from two event datamodels, so it can be
    boxed to Python."""
    ty_event = DpctlSyclEvent()
    tupty = types.Tuple([ty_event, ty_event])

    lltupty = ctx.get_value_type(tupty)
    tup = cgutils.get_null_value(lltupty)
    tup = builder.insert_value(tup, event1, 0)
    tup = builder.insert_value(tup, event2, 1)

    return tup


@intrinsic(target="cpu")
def _submit_kernel_async(
    typingctx,
    ty_kernel_fn: Dispatcher,
    ty_index_space: Union[RangeType, NdRangeType],
    ty_kernel_args_tuple: UniTuple,
):
    """Generates IR code for call_kernel_async dpjit function."""
    return _submit_kernel(
        typingctx,
        ty_kernel_fn,
        ty_index_space,
        ty_kernel_args_tuple,
        sync=False,
    )


@intrinsic(target="cpu")
def _submit_kernel_sync(
    typingctx,
    ty_kernel_fn: Dispatcher,
    ty_index_space: Union[RangeType, NdRangeType],
    ty_kernel_args_tuple: UniTuple,
):
    """Generates IR code for call_kernel dpjit function."""
    return _submit_kernel(
        typingctx,
        ty_kernel_fn,
        ty_index_space,
        ty_kernel_args_tuple,
        sync=True,
    )


def _submit_kernel(
    typingctx,  # pylint: disable=W0613
    ty_kernel_fn: Dispatcher,
    ty_index_space: Union[RangeType, NdRangeType],
    ty_kernel_args_tuple: UniTuple,
    sync: bool,
):
    """Generates IR code for call_kernel_{async|sync} dpjit function.

    The intrinsic first compiles the kernel function to SPIRV, and then to a
    sycl kernel bundle. The arguments to the kernel are also packed into
    flattened arrays and the sycl queue to which the kernel will be submitted
    extracted from the args. Finally, the actual kernel is extracted from the
    kernel bundle and submitted to the sycl queue.

    If sync set to False, it acquires memory infos from kernel arguments to
    prevent garbage collection on them. Then it schedules host task to release
    that arguments and unblock garbage collection. Tuple of host task and device
    tasks are returned.
    """
    # signature of this intrinsic
    ty_return = types.void
    if not sync:
        ty_event = DpctlSyclEvent()
        ty_return = types.Tuple([ty_event, ty_event])

    sig = ty_return(ty_kernel_fn, ty_index_space, ty_kernel_args_tuple)
    kernel_sig = types.void(*ty_kernel_args_tuple)
    # ty_kernel_fn is type specific to exact function, so we can get function
    # directly from type and compile it. Thats why we don't need to get it in
    # codegen
    kernel_dispatcher: KernelDispatcher = ty_kernel_fn.dispatcher
    kernel_dispatcher.compile(kernel_sig)
    kernel_module: kl.SPIRVKernelModule = kernel_dispatcher.get_overload_kcres(
        kernel_sig
    ).kernel_device_ir_module
    kernel_targetctx: DpexKernelTargetContext = kernel_dispatcher.targetctx

    def codegen(
        cgctx: CPUContext, builder: llvmir.IRBuilder, sig, llargs: list
    ):
        ty_index_space: Union[RangeType, NdRangeType] = sig.args[1]
        ll_index_space: llvmir.Instruction = llargs[1]
        ty_kernel_args_tuple: UniTuple = sig.args[2]
        ll_kernel_args_tuple: llvmir.Instruction = llargs[2]

        kl_builder = kl.KernelLaunchIRBuilder(
            cgctx,
            builder,
            kernel_targetctx.data_model_manager,
        )
        kl_builder.set_range_from_indexer(
            ty_indexer_arg=ty_index_space,
            ll_index_arg=ll_index_space,
        )
        kl_builder.set_arguments_form_tuple(
            ty_kernel_args_tuple, ll_kernel_args_tuple
        )
        kl_builder.set_queue_from_arguments()
        kl_builder.set_kernel_from_spirv(kernel_module)
        kl_builder.set_dependant_event_list([])
        device_event_ref = kl_builder.submit()

        if not sync:
            host_event_ref = kl_builder.acquire_meminfo_and_submit_release()

            return wrap_event_reference_tuple(
                cgctx,
                builder,
                wrap_event_reference(cgctx, builder, host_event_ref),
                wrap_event_reference(cgctx, builder, device_event_ref),
            )

        sycl.dpctl_event_wait(builder, device_event_ref)
        sycl.dpctl_event_delete(builder, device_event_ref)

        return None

    return sig, codegen


@dpjit
def call_kernel(kernel_fn, index_space, *kernel_args) -> None:
    """Calls a numba_dpex.kernel decorated function from CPython or from another
    dpjit function. Kernel execution happens in syncronous way, so the thread
    will be blocked till the kernel done exectuion.

    Args:
        kernel_fn (numba_dpex.experimental.KernelDispatcher): A
        numba_dpex.kernel decorated function that is compiled to a
        KernelDispatcher by numba_dpex.
        index_space (Range | NdRange): A numba_dpex.Range or numba_dpex.NdRange
        type object that specifies the index space for the kernel.
        kernel_args : List of objects that are passed to the numba_dpex.kernel
        decorated function.
    """
    _submit_kernel_sync(  # pylint: disable=E1120
        kernel_fn,
        index_space,
        kernel_args,
    )


@dpjit
def call_kernel_async(
    kernel_fn, index_space, *kernel_args
) -> tuple[dpctl.SyclEvent, dpctl.SyclEvent]:
    """Calls a numba_dpex.kernel decorated function from CPython or from another
    dpjit function. Kernel execution happens in asyncronous way, so the thread
    will not be blocked till the kernel done exectuion. That means that it is
    user responsiblity to properly use any memory used by kernel until the
    kernel execution is completed.

    Args:
        kernel_fn (numba_dpex.experimental.KernelDispatcher): A
        numba_dpex.kernel decorated function that is compiled to a
        KernelDispatcher by numba_dpex.
        index_space (Range | NdRange): A numba_dpex.Range or numba_dpex.NdRange
        type object that specifies the index space for the kernel.
        kernel_args : List of objects that are passed to the numba_dpex.kernel
        decorated function.

    Returns:
        pair of host event and device event. Host event represent host task
        that releases use of any kernel argument so it can be deallocated.
        This task may be executed only after device task is done.
    """
    return _submit_kernel_async(  # pylint: disable=E1120
        kernel_fn,
        index_space,
        kernel_args,
    )