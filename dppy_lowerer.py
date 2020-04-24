from __future__ import print_function, division, absolute_import


import ast
import copy
from collections import OrderedDict
import linecache
import os
import sys
import numpy as np

import llvmlite.llvmpy.core as lc
import llvmlite.ir.values as liv
import llvmlite.ir as lir
import llvmlite.binding as lb

import numba
from .. import compiler, ir, types, six, cgutils, sigutils, lowering, parfor, funcdesc
from numba.ir_utils import (add_offset_to_labels, replace_var_names,
                            remove_dels, legalize_names, mk_unique_var,
                            rename_labels, get_name_var_table, visit_vars_inner,
                            guard, find_callname, remove_dead,
                            get_call_table, is_pure, build_definitions, get_np_ufunc_typ,
                            get_unused_var_name, find_potential_aliases,
                            visit_vars_inner, is_const_call)
from numba.analysis import (compute_use_defs, compute_live_map,
                            compute_dead_maps, compute_cfg_from_blocks,
                            ir_extension_usedefs, _use_defs_result)
from ..typing import signature
from numba import config, typeinfer, dppy
from numba.targets.cpu import ParallelOptions
from numba.six import exec_
import types as pytypes
import operator

import warnings
from ..errors import NumbaParallelSafetyWarning

from numba.dppy.target import SPIR_GENERIC_ADDRSPACE
from .dppy_lowerer_2 import _lower_parfor_dppy_no_gufunc
import dppy.core as driver


def replace_var_with_array_in_block(vars, block, typemap, calltypes):
    new_block = []
    for inst in block.body:
        if isinstance(inst, ir.Assign) and inst.target.name in vars:
            const_node = ir.Const(0, inst.loc)
            const_var = ir.Var(inst.target.scope, mk_unique_var("$const_ind_0"), inst.loc)
            typemap[const_var.name] = types.uintp
            const_assign = ir.Assign(const_node, const_var, inst.loc)
            new_block.append(const_assign)

            setitem_node = ir.SetItem(inst.target, const_var, inst.value, inst.loc)
            calltypes[setitem_node] = signature(
                types.none, types.npytypes.Array(typemap[inst.target.name], 1, "C"), types.intp, typemap[inst.target.name])
            new_block.append(setitem_node)
            continue
        elif isinstance(inst, parfor.Parfor):
            replace_var_with_array_internal(vars, {0: inst.init_block}, typemap, calltypes)
            replace_var_with_array_internal(vars, inst.loop_body, typemap, calltypes)

        new_block.append(inst)
    return new_block

def replace_var_with_array_internal(vars, loop_body, typemap, calltypes):
    for label, block in loop_body.items():
        block.body = replace_var_with_array_in_block(vars, block, typemap, calltypes)

def replace_var_with_array(vars, loop_body, typemap, calltypes):
    replace_var_with_array_internal(vars, loop_body, typemap, calltypes)
    for v in vars:
        el_typ = typemap[v]
        typemap.pop(v, None)
        typemap[v] = types.npytypes.Array(el_typ, 1, "C")


def wrap_loop_body(loop_body):
    blocks = loop_body.copy()  # shallow copy is enough
    first_label = min(blocks.keys())
    last_label = max(blocks.keys())
    loc = blocks[last_label].loc
    blocks[last_label].body.append(ir.Jump(first_label, loc))
    return blocks

def unwrap_loop_body(loop_body):
    last_label = max(loop_body.keys())
    loop_body[last_label].body = loop_body[last_label].body[:-1]

def add_to_def_once_sets(a_def, def_once, def_more):
    '''If the variable is already defined more than once, do nothing.
       Else if defined exactly once previously then transition this
       variable to the defined more than once set (remove it from
       def_once set and add to def_more set).
       Else this must be the first time we've seen this variable defined
       so add to def_once set.
    '''
    if a_def in def_more:
        pass
    elif a_def in def_once:
        def_more.add(a_def)
        def_once.remove(a_def)
    else:
        def_once.add(a_def)

def compute_def_once_block(block, def_once, def_more, getattr_taken, typemap, module_assigns):
    '''Effect changes to the set of variables defined once or more than once
       for a single block.
       block - the block to process
       def_once - set of variable names known to be defined exactly once
       def_more - set of variable names known to be defined more than once
       getattr_taken - dict mapping variable name to tuple of object and attribute taken
       module_assigns - dict mapping variable name to the Global that they came from
    '''
    # The only "defs" occur in assignments, so find such instructions.
    assignments = block.find_insts(ir.Assign)
    # For each assignment...
    for one_assign in assignments:
        # Get the LHS/target of the assignment.
        a_def = one_assign.target.name
        # Add variable to def sets.
        add_to_def_once_sets(a_def, def_once, def_more)

        rhs = one_assign.value
        if isinstance(rhs, ir.Global):
            # Remember assignments of the form "a = Global(...)"
            # Is this a module?
            if isinstance(rhs.value, pytypes.ModuleType):
                module_assigns[a_def] = rhs.value.__name__
        if isinstance(rhs, ir.Expr) and rhs.op == 'getattr' and rhs.value.name in def_once:
            # Remember assignments of the form "a = b.c"
            getattr_taken[a_def] = (rhs.value.name, rhs.attr)
        if isinstance(rhs, ir.Expr) and rhs.op == 'call' and rhs.func.name in getattr_taken:
            # If "a" is being called then lookup the getattr definition of "a"
            # as above, getting the module variable "b" (base_obj)
            # and the attribute "c" (base_attr).
            base_obj, base_attr = getattr_taken[rhs.func.name]
            if base_obj in module_assigns:
                # If we know the definition of the module variable then get the module
                # name from module_assigns.
                base_mod_name = module_assigns[base_obj]
                if not is_const_call(base_mod_name, base_attr):
                    # Calling a method on an object could modify the object and is thus
                    # like a def of that object.  We call is_const_call to see if this module/attribute
                    # combination is known to not modify the module state.  If we don't know that
                    # the combination is safe then we have to assume there could be a modification to
                    # the module and thus add the module variable as defined more than once.
                    add_to_def_once_sets(base_obj, def_once, def_more)
            else:
                # Assume the worst and say that base_obj could be modified by the call.
                add_to_def_once_sets(base_obj, def_once, def_more)
        if isinstance(rhs, ir.Expr) and rhs.op == 'call':
            # If a mutable object is passed to a function, then it may be changed and
            # therefore can't be hoisted.
            # For each argument to the function...
            for argvar in rhs.args:
                # Get the argument's type.
                if isinstance(argvar, ir.Var):
                    argvar = argvar.name
                avtype = typemap[argvar]
                # If that type doesn't have a mutable attribute or it does and it's set to
                # not mutable then this usage is safe for hoisting.
                if getattr(avtype, 'mutable', False):
                    # Here we have a mutable variable passed to a function so add this variable
                    # to the def lists.
                    add_to_def_once_sets(argvar, def_once, def_more)

def compute_def_once_internal(loop_body, def_once, def_more, getattr_taken, typemap, module_assigns):
    '''Compute the set of variables defined exactly once in the given set of blocks
       and use the given sets for storing which variables are defined once, more than
       once and which have had a getattr call on them.
    '''
    # For each block...
    for label, block in loop_body.items():
        # Scan this block and effect changes to def_once, def_more, and getattr_taken
        # based on the instructions in that block.
        compute_def_once_block(block, def_once, def_more, getattr_taken, typemap, module_assigns)
        # Have to recursively process parfors manually here.
        for inst in block.body:
            if isinstance(inst, parfor.Parfor):
                # Recursively compute for the parfor's init block.
                compute_def_once_block(inst.init_block, def_once, def_more, getattr_taken, typemap, module_assigns)
                # Recursively compute for the parfor's loop body.
                compute_def_once_internal(inst.loop_body, def_once, def_more, getattr_taken, typemap, module_assigns)

def compute_def_once(loop_body, typemap):
    '''Compute the set of variables defined exactly once in the given set of blocks.
    '''
    def_once = set()   # set to hold variables defined exactly once
    def_more = set()   # set to hold variables defined more than once
    getattr_taken = {}
    module_assigns = {}
    compute_def_once_internal(loop_body, def_once, def_more, getattr_taken, typemap, module_assigns)
    return def_once

def find_vars(var, varset):
    assert isinstance(var, ir.Var)
    varset.add(var.name)
    return var

def _hoist_internal(inst, dep_on_param, call_table, hoisted, not_hoisted,
                    typemap, stored_arrays):
    if inst.target.name in stored_arrays:
        not_hoisted.append((inst, "stored array"))
        if config.DEBUG_ARRAY_OPT >= 1:
            print("Instruction", inst, " could not be hoisted because the created array is stored.")
        return False

    uses = set()
    visit_vars_inner(inst.value, find_vars, uses)
    diff = uses.difference(dep_on_param)
    if config.DEBUG_ARRAY_OPT >= 1:
        print("_hoist_internal:", inst, "uses:", uses, "diff:", diff)
    if len(diff) == 0 and is_pure(inst.value, None, call_table):
        if config.DEBUG_ARRAY_OPT >= 1:
            print("Will hoist instruction", inst, typemap[inst.target.name])
        hoisted.append(inst)
        if not isinstance(typemap[inst.target.name], types.npytypes.Array):
            dep_on_param += [inst.target.name]
        return True
    else:
        if len(diff) > 0:
            not_hoisted.append((inst, "dependency"))
            if config.DEBUG_ARRAY_OPT >= 1:
                print("Instruction", inst, " could not be hoisted because of a dependency.")
        else:
            not_hoisted.append((inst, "not pure"))
            if config.DEBUG_ARRAY_OPT >= 1:
                print("Instruction", inst, " could not be hoisted because it isn't pure.")
    return False

def find_setitems_block(setitems, itemsset, block, typemap):
    for inst in block.body:
        if isinstance(inst, ir.StaticSetItem) or isinstance(inst, ir.SetItem):
            setitems.add(inst.target.name)
            # If we store a non-mutable object into an array then that is safe to hoist.
            # If the stored object is mutable and you hoist then multiple entries in the
            # outer array could reference the same object and changing one index would then
            # change other indices.
            if getattr(typemap[inst.value.name], "mutable", False):
                itemsset.add(inst.value.name)
        elif isinstance(inst, parfor.Parfor):
            find_setitems_block(setitems, itemsset, inst.init_block, typemap)
            find_setitems_body(setitems, itemsset, inst.loop_body, typemap)

def find_setitems_body(setitems, itemsset, loop_body, typemap):
    """
      Find the arrays that are written into (goes into setitems) and the
      mutable objects (mostly arrays) that are written into other arrays
      (goes into itemsset).
    """
    for label, block in loop_body.items():
        find_setitems_block(setitems, itemsset, block, typemap)

def hoist(parfor_params, loop_body, typemap, wrapped_blocks):
    dep_on_param = copy.copy(parfor_params)
    hoisted = []
    not_hoisted = []

    # Compute the set of variable defined exactly once in the loop body.
    def_once = compute_def_once(loop_body, typemap)
    (call_table, reverse_call_table) = get_call_table(wrapped_blocks)

    setitems = set()
    itemsset = set()
    find_setitems_body(setitems, itemsset, loop_body, typemap)
    dep_on_param = list(set(dep_on_param).difference(setitems))
    if config.DEBUG_ARRAY_OPT >= 1:
        print("hoist - def_once:", def_once, "setitems:", setitems, "itemsset:", itemsset, "dep_on_param:", dep_on_param, "parfor_params:", parfor_params)

    for label, block in loop_body.items():
        new_block = []
        for inst in block.body:
            if isinstance(inst, ir.Assign) and inst.target.name in def_once:
                if _hoist_internal(inst, dep_on_param, call_table,
                                   hoisted, not_hoisted, typemap, itemsset):
                    # don't add this instruction to the block since it is
                    # hoisted
                    continue
            elif isinstance(inst, parfor.Parfor):
                new_init_block = []
                if config.DEBUG_ARRAY_OPT >= 1:
                    print("parfor")
                    inst.dump()
                for ib_inst in inst.init_block.body:
                    if (isinstance(ib_inst, ir.Assign) and
                        ib_inst.target.name in def_once):
                        if _hoist_internal(ib_inst, dep_on_param, call_table,
                                           hoisted, not_hoisted, typemap, itemsset):
                            # don't add this instuction to the block since it is hoisted
                            continue
                    new_init_block.append(ib_inst)
                inst.init_block.body = new_init_block

            new_block.append(inst)
        block.body = new_block
    return hoisted, not_hoisted

def redtyp_is_scalar(redtype):
    return not isinstance(redtype, types.npytypes.Array)

def redtyp_to_redarraytype(redtyp):
    """Go from a reducation variable type to a reduction array type used to hold
       per-worker results.
    """
    redarrdim = 1
    # If the reduction type is an array then allocate reduction array with ndim+1 dimensions.
    if isinstance(redtyp, types.npytypes.Array):
        redarrdim += redtyp.ndim
        # We don't create array of array but multi-dimensional reduciton array with same dtype.
        redtyp = redtyp.dtype
    return types.npytypes.Array(redtyp, redarrdim, "C")

def redarraytype_to_sig(redarraytyp):
    """Given a reduction array type, find the type of the reduction argument to the gufunc.
       Scalar and 1D array reduction both end up with 1D gufunc param type since scalars have to
       be passed as arrays.
    """
    assert isinstance(redarraytyp, types.npytypes.Array)
    return types.npytypes.Array(redarraytyp.dtype, max(1, redarraytyp.ndim - 1), redarraytyp.layout)

def legalize_names_with_typemap(names, typemap):
    """ We use ir_utils.legalize_names to replace internal IR variable names
        containing illegal characters (e.g. period) with a legal character
        (underscore) so as to create legal variable names.
        The original variable names are in the typemap so we also
        need to add the legalized name to the typemap as well.
    """
    outdict = legalize_names(names)
    # For each pair in the dict of legalized names...
    for x, y in outdict.items():
        # If the name had some legalization change to it...
        if x != y:
            # Set the type of the new name the same as the type of the old name.
            typemap[y] = typemap[x]
    return outdict



def to_scalar_from_0d(x):
    if isinstance(x, types.ArrayCompatible):
        if x.ndim == 0:
            return x.dtype
    return x


def _create_gufunc_for_parfor_body(
        lowerer,
        parfor,
        target,
        typemap,
        typingctx,
        targetctx,
        flags,
        loop_ranges,
        locals,
        has_aliases,
        index_var_typ,
        races):
    '''
    Takes a parfor and creates a gufunc function for its body.
    There are two parts to this function.
    1) Code to iterate across the iteration space as defined by the schedule.
    2) The parfor body that does the work for a single point in the iteration space.
    Part 1 is created as Python text for simplicity with a sentinel assignment to mark the point
    in the IR where the parfor body should be added.
    This Python text is 'exec'ed into existence and its IR retrieved with run_frontend.
    The IR is scanned for the sentinel assignment where that basic block is split and the IR
    for the parfor body inserted.
    '''

    loc = parfor.init_block.loc

    # The parfor body and the main function body share ir.Var nodes.
    # We have to do some replacements of Var names in the parfor body to make them
    # legal parameter names.  If we don't copy then the Vars in the main function also
    # would incorrectly change their name.
    loop_body = copy.copy(parfor.loop_body)
    remove_dels(loop_body)

    parfor_dim = len(parfor.loop_nests)
    loop_indices = [l.index_variable.name for l in parfor.loop_nests]

    # Get all the parfor params.
    parfor_params = parfor.params

    for start, stop, step in loop_ranges:
        if isinstance(start, ir.Var):
            parfor_params.add(start.name)
        if isinstance(stop, ir.Var):
            parfor_params.add(stop.name)

    # Get just the outputs of the parfor.
    parfor_outputs = numba.parfor.get_parfor_outputs(parfor, parfor_params)
    # Get all parfor reduction vars, and operators.
    typemap = lowerer.fndesc.typemap
    parfor_redvars, parfor_reddict = numba.parfor.get_parfor_reductions(
        lowerer.func_ir, parfor, parfor_params, lowerer.fndesc.calltypes)
    has_reduction = False if len(parfor_redvars) == 0 else True
    # Compute just the parfor inputs as a set difference.
    parfor_inputs = sorted(
        list(
            set(parfor_params) -
            set(parfor_outputs) -
            set(parfor_redvars)))

    races = races.difference(set(parfor_redvars))
    for race in races:
        msg = ("Variable %s used in parallel loop may be written "
               "to simultaneously by multiple workers and may result "
               "in non-deterministic or unintended results." % race)
        warnings.warn(NumbaParallelSafetyWarning(msg, loc))
    replace_var_with_array(races, loop_body, typemap, lowerer.fndesc.calltypes)

    if config.DEBUG_ARRAY_OPT >= 1:
        print("parfor_params = ", parfor_params, type(parfor_params))
        print("parfor_outputs = ", parfor_outputs, type(parfor_outputs))
        print("parfor_inputs = ", parfor_inputs, type(parfor_inputs))
        print("parfor_redvars = ", parfor_redvars, type(parfor_redvars))
        print("parfor_reddict = ", parfor_reddict, type(parfor_reddict))

    # Reduction variables are represented as arrays, so they go under
    # different names.
    parfor_redarrs = []
    parfor_local_redarrs = []
    parfor_red_arg_types = []
    parfor_redvar_types = []
    for var in parfor_redvars:
        arr = var + "_arr"
        parfor_redarrs.append(arr)
        parfor_redvar_types.append(typemap[var])
        redarraytype = redtyp_to_redarraytype(typemap[var])
        parfor_red_arg_types.append(redarraytype)
        redarrsig = redarraytype_to_sig(redarraytype)
        if arr in typemap:
            assert(typemap[arr] == redarrsig)
        else:
            typemap[arr] = redarrsig

        # target will aways be spirv
        #if target=='spirv':
        local_arr = var + "_local_arr"
        parfor_local_redarrs.append(local_arr)
        if local_arr in typemap:
            assert(typemap[local_arr] == redarrsig)
        else:
            typemap[local_arr] = redarrsig

    # Reorder all the params so that inputs go first then outputs.
    parfor_params = parfor_inputs + parfor_outputs

    #if target=='spirv':
    def addrspace_from(params, def_addr):
        addrspaces = []
        for p in params:
            if isinstance(to_scalar_from_0d(typemap[p]),
                          types.npytypes.Array):
                addrspaces.append(def_addr)
            else:
                addrspaces.append(None)
        return addrspaces

    #print(dir(numba.dppy))
    #print(numba.dppy.compiler.DEBUG)
    addrspaces = addrspace_from(parfor_params, numba.dppy.target.SPIR_GLOBAL_ADDRSPACE)

    # Pass in the initial value as a simple var.
    parfor_params.extend(parfor_redvars)
    parfor_params.extend(parfor_local_redarrs)
    addrspaces.extend(addrspace_from(parfor_redvars, dppy.target.SPIR_GENERIC_ADDRSPACE))
    addrspaces.extend(addrspace_from(parfor_local_redarrs, dppy.target.SPIR_LOCAL_ADDRSPACE))
    parfor_params.extend(parfor_redarrs)

    #if target=='spirv':
    addrspaces.extend(addrspace_from(parfor_redarrs, dppy.target.SPIR_GLOBAL_ADDRSPACE))

    if config.DEBUG_ARRAY_OPT >= 1:
        print("parfor_params = ", parfor_params, type(parfor_params))
        print("loop_indices = ", loop_indices, type(loop_indices))
        print("loop_body = ", loop_body, type(loop_body))
        _print_body(loop_body)

    # Some Var are not legal parameter names so create a dict of potentially illegal
    # param name to guaranteed legal name.
    param_dict = legalize_names_with_typemap(parfor_params + parfor_redvars, typemap)
    if config.DEBUG_ARRAY_OPT >= 1:
        print("param_dict = ", sorted(param_dict.items()), type(param_dict))

    # Some loop_indices are not legal parameter names so create a dict of potentially illegal
    # loop index to guaranteed legal name.
    ind_dict = legalize_names_with_typemap(loop_indices, typemap)
    # Compute a new list of legal loop index names.
    legal_loop_indices = [ind_dict[v] for v in loop_indices]
    if config.DEBUG_ARRAY_OPT >= 1:
        print("ind_dict = ", sorted(ind_dict.items()), type(ind_dict))
        print(
            "legal_loop_indices = ",
            legal_loop_indices,
            type(legal_loop_indices))
        for pd in parfor_params:
            print("pd = ", pd)
            print("pd type = ", typemap[pd], type(typemap[pd]))

    # Get the types of each parameter.
    param_types = [to_scalar_from_0d(typemap[v]) for v in parfor_params]

    param_types_addrspaces = copy.copy(param_types)

    # Calculate types of args passed to gufunc.
    func_arg_types = [typemap[v] for v in (parfor_inputs + parfor_outputs)]
    #if target=='spirv':
    assert(len(param_types_addrspaces) == len(addrspaces))
    for i in range(len(param_types_addrspaces)):
        if addrspaces[i] is not None:
            print("before:", id(param_types_addrspaces[i]))
            assert(isinstance(param_types_addrspaces[i], types.npytypes.Array))
            param_types_addrspaces[i] = param_types_addrspaces[i].copy(addrspace=addrspaces[i])
            print("setting param type", i, param_types[i], id(param_types[i]), "to addrspace", param_types_addrspaces[i].addrspace)
    # the output reduction array has the same types as the local reduction reduction arrays
    func_arg_types.extend(parfor_redvar_types)
    func_arg_types.extend(parfor_red_arg_types)

    def print_arg_with_addrspaces(args):
        for a in args:
            print(a, type(a))
            if isinstance(a, types.npytypes.Array):
                print("addrspace:", a.addrspace)

    if config.DEBUG_ARRAY_OPT >= 1:
        print_arg_with_addrspaces(param_types)
        print("func_arg_types = ", func_arg_types, type(func_arg_types))

    # Replace illegal parameter names in the loop body with legal ones.
    replace_var_names(loop_body, param_dict)
    # remember the name before legalizing as the actual arguments
    parfor_args = parfor_params
    # Change parfor_params to be legal names.
    parfor_params = [param_dict[v] for v in parfor_params]
    parfor_params_orig = parfor_params

    parfor_params = []
    ascontig = False
    for pindex in range(len(parfor_params_orig)):
        if (ascontig and
            pindex < len(parfor_inputs) and
            isinstance(param_types[pindex], types.npytypes.Array)):
            parfor_params.append(parfor_params_orig[pindex]+"param")
        else:
            parfor_params.append(parfor_params_orig[pindex])

    # Change parfor body to replace illegal loop index vars with legal ones.
    replace_var_names(loop_body, ind_dict)
    loop_body_var_table = get_name_var_table(loop_body)
    sentinel_name = get_unused_var_name("__sentinel__", loop_body_var_table)

    if config.DEBUG_ARRAY_OPT >= 1:
        print(
            "legal parfor_params = ",
            parfor_params,
            type(parfor_params))


    # Determine the unique names of the scheduling and gufunc functions.
    # sched_func_name = "__numba_parfor_sched_%s" % (hex(hash(parfor)).replace("-", "_"))
    gufunc_name = "__numba_parfor_gufunc_%s" % (
        hex(hash(parfor)).replace("-", "_"))
    if config.DEBUG_ARRAY_OPT:
        # print("sched_func_name ", type(sched_func_name), sched_func_name)
        print("gufunc_name ", type(gufunc_name), gufunc_name)

    gufunc_txt = ""

    # Create the gufunc function.
    gufunc_txt += "def " + gufunc_name
    gufunc_txt += "(" + (", ".join(parfor_params)) + "):\n"

#    for pindex in range(len(parfor_inputs)):
#        if ascontig and isinstance(param_types[pindex], types.npytypes.Array):
#            gufunc_txt += ("    " + parfor_params_orig[pindex]
#                + " = np.ascontiguousarray(" + parfor_params[pindex] + ")\n")

    #if target=='spirv':
        # Intentionally do nothing here for reduction initialization in gufunc.
        # We don't have to do anything because we pass in the initial reduction
        # var value as a param.
    reduction_sentinel_name = get_unused_var_name("__reduction_sentinel__", loop_body_var_table)

    #if target=='spirv':
    for eachdim in range(parfor_dim):
        gufunc_txt += "    " + legal_loop_indices[eachdim] + " = " + "dppy.get_global_id(" + str(eachdim) + ")\n"
    if has_reduction:
        assert(parfor_dim == 1)
        gufunc_txt += "    gufunc_numItems = dppy.get_local_size(0)\n"
        gufunc_txt += "    gufunc_tnum = dppy.get_local_id(0)\n"
        gufunc_txt += "    gufunc_wgNum = dppy.get_local_size(0)\n"

    # Add the sentinel assignment so that we can find the loop body position
    # in the IR.
    #if target=='spirv':
    gufunc_txt += "    "
    gufunc_txt += sentinel_name + " = 0\n"

    redargstartdim = {}
    #if target=='spirv':
    if has_reduction:
        #if target == 'spirv':
        for var, local_arr in zip(parfor_redvars, parfor_local_redarrs):
            if redtyp_is_scalar(typemap[var]):
                gufunc_txt += "    " + param_dict[local_arr] + \
                    "[gufunc_tnum] = " + param_dict[var] + "\n"
            else:
                # After the gufunc loops, copy the accumulated temp array back to reduction array with ":"
                gufunc_txt += "    " + param_dict[local_arr] + \
                    "[gufunc_tnum, :] = " + param_dict[var] + "[:]\n"

        gufunc_txt += "    gufunc_red_offset = 1\n"
        gufunc_txt += "    while gufunc_red_offset < gufunc_numItems:\n"
        gufunc_txt += "        mask = (2 * gufunc_red_offset) - 1\n"
        gufunc_txt += "        dppy.barrier(dppy.enums.CLK_LOCAL_MEM_FENCE)\n"
        gufunc_txt += "        if (gufunc_tnum & mask) == 0:\n"
        gufunc_txt += "            " + reduction_sentinel_name + " = 0\n"
#            gufunc_txt += "            # red_result[gufunc_tnum] = red_result[gufunc_tnum] (reduction_operator) red_result[gufunc_tnum+offset]\n"
#            gufunc_txt += "            pass\n"
        gufunc_txt += "        gufunc_red_offset *= 2\n"
        gufunc_txt += "    dppy.barrier(dppy.enums.CLK_LOCAL_MEM_FENCE)\n"
        gufunc_txt += "    if gufunc_tnum == 0:\n"
        for arr, var, local_arr in zip(parfor_redarrs, parfor_redvars, parfor_local_redarrs):
            # After the gufunc loops, copy the accumulated temp value back to reduction array.
            if redtyp_is_scalar(typemap[var]):
                gufunc_txt += "        " + param_dict[arr] + \
                    "[gufunc_wgNum] = " + param_dict[local_arr] + "[0]\n"
                redargstartdim[arr] = 1
            else:
                # After the gufunc loops, copy the accumulated temp array back to reduction array with ":"
                gufunc_txt += "        " + param_dict[arr] + \
                    "[gufunc_wgNum, :] = " + param_dict[local_arr] + "[0, :]\n"
                redargstartdim[arr] = 0

    # gufunc returns nothing
    gufunc_txt += "    return None\n"

    if config.DEBUG_ARRAY_OPT:
        print("gufunc_txt = ", type(gufunc_txt), "\n", gufunc_txt)
        sys.stdout.flush()
    # Force gufunc outline into existence.
    globls = {"np": np, "numba": numba}
    if target=='spirv':
        globls["dppy"] = dppy
    locls = {}
    exec_(gufunc_txt, globls, locls)
    gufunc_func = locls[gufunc_name]

    if config.DEBUG_ARRAY_OPT:
        print("gufunc_func = ", type(gufunc_func), "\n", gufunc_func)
    # Get the IR for the gufunc outline.
    gufunc_ir = compiler.run_frontend(gufunc_func)

    if config.DEBUG_ARRAY_OPT:
        print("gufunc_ir dump ", type(gufunc_ir))
        gufunc_ir.dump()
        print("loop_body dump ", type(loop_body))
        _print_body(loop_body)

    # rename all variables in gufunc_ir afresh
    var_table = get_name_var_table(gufunc_ir.blocks)
    new_var_dict = {}
    reserved_names = [sentinel_name] + \
        list(param_dict.values()) + legal_loop_indices
    for name, var in var_table.items():
        if not (name in reserved_names):
            new_var_dict[name] = mk_unique_var(name)
    replace_var_names(gufunc_ir.blocks, new_var_dict)
    if config.DEBUG_ARRAY_OPT:
        print("gufunc_ir dump after renaming ")
        gufunc_ir.dump()

    prs_dict = {}
    pss_dict = {}
    pspmd_dict = {}

    gufunc_param_types = param_types

    if config.DEBUG_ARRAY_OPT:
        print(
            "gufunc_param_types = ",
            type(gufunc_param_types),
            "\n",
            gufunc_param_types)

    gufunc_stub_last_label = max(gufunc_ir.blocks.keys()) + 1

    # Add gufunc stub last label to each parfor.loop_body label to prevent
    # label conflicts.
    loop_body = add_offset_to_labels(loop_body, gufunc_stub_last_label)
    # new label for splitting sentinel block
    new_label = max(loop_body.keys()) + 1

    # If enabled, add a print statement after every assignment.
    if config.DEBUG_ARRAY_OPT_RUNTIME:
        for label, block in loop_body.items():
            new_block = block.copy()
            new_block.clear()
            loc = block.loc
            scope = block.scope
            for inst in block.body:
                new_block.append(inst)
                # Append print after assignment
                if isinstance(inst, ir.Assign):
                    # Only apply to numbers
                    if typemap[inst.target.name] not in types.number_domain:
                        continue

                    # Make constant string
                    strval = "{} =".format(inst.target.name)
                    strconsttyp = types.StringLiteral(strval)

                    lhs = ir.Var(scope, mk_unique_var("str_const"), loc)
                    assign_lhs = ir.Assign(value=ir.Const(value=strval, loc=loc),
                                           target=lhs, loc=loc)
                    typemap[lhs.name] = strconsttyp
                    new_block.append(assign_lhs)

                    # Make print node
                    print_node = ir.Print(args=[lhs, inst.target], vararg=None, loc=loc)
                    new_block.append(print_node)
                    sig = numba.typing.signature(types.none,
                                           typemap[lhs.name],
                                           typemap[inst.target.name])
                    lowerer.fndesc.calltypes[print_node] = sig
            loop_body[label] = new_block

    if config.DEBUG_ARRAY_OPT:
        print("parfor loop body")
        _print_body(loop_body)

    wrapped_blocks = wrap_loop_body(loop_body)
    hoisted, not_hoisted = hoist(parfor_params, loop_body, typemap, wrapped_blocks)
    start_block = gufunc_ir.blocks[min(gufunc_ir.blocks.keys())]
    start_block.body = start_block.body[:-1] + hoisted + [start_block.body[-1]]
    unwrap_loop_body(loop_body)

    # store hoisted into diagnostics
    diagnostics = lowerer.metadata['parfor_diagnostics']
    diagnostics.hoist_info[parfor.id] = {'hoisted': hoisted,
                                         'not_hoisted': not_hoisted}

    if config.DEBUG_ARRAY_OPT:
        print("After hoisting")
        _print_body(loop_body)

    # Search all the block in the gufunc outline for the sentinel assignment.
    for label, block in gufunc_ir.blocks.items():
        for i, inst in enumerate(block.body):
            if isinstance(inst, ir.Assign) and inst.target.name == sentinel_name:
                # We found the sentinel assignment.
                loc = inst.loc
                scope = block.scope
                # split block across __sentinel__
                # A new block is allocated for the statements prior to the sentinel
                # but the new block maintains the current block label.
                prev_block = ir.Block(scope, loc)
                prev_block.body = block.body[:i]

                # The current block is used for statements after the sentinel.
                block.body = block.body[i + 1:]
                # But the current block gets a new label.
                body_first_label = min(loop_body.keys())

                # The previous block jumps to the minimum labelled block of the
                # parfor body.
                prev_block.append(ir.Jump(body_first_label, loc))
                # Add all the parfor loop body blocks to the gufunc function's
                # IR.
                for (l, b) in loop_body.items():
                    gufunc_ir.blocks[l] = b
                body_last_label = max(loop_body.keys())
                gufunc_ir.blocks[new_label] = block
                gufunc_ir.blocks[label] = prev_block
                # Add a jump from the last parfor body block to the block containing
                # statements after the sentinel.
                gufunc_ir.blocks[body_last_label].append(
                    ir.Jump(new_label, loc))
                break
        else:
            continue
        break

    if has_reduction:
        # Search all the block in the gufunc outline for the reduction sentinel assignment.
        for label, block in gufunc_ir.blocks.items():
            for i, inst in enumerate(block.body):
                if isinstance(
                        inst,
                        ir.Assign) and inst.target.name == reduction_sentinel_name:
                    # We found the reduction sentinel assignment.
                    loc = inst.loc
                    scope = block.scope
                    # split block across __sentinel__
                    # A new block is allocated for the statements prior to the sentinel
                    # but the new block maintains the current block label.
                    prev_block = ir.Block(scope, loc)
                    prev_block.body = block.body[:i]
                    # The current block is used for statements after the sentinel.
                    block.body = block.body[i + 1:]
                    # But the current block gets a new label.
                    body_first_label = min(loop_body.keys())

                    # The previous block jumps to the minimum labelled block of the
                    # parfor body.
                    prev_block.append(ir.Jump(body_first_label, loc))
                    # Add all the parfor loop body blocks to the gufunc function's
                    # IR.
                    for (l, b) in loop_body.items():
                        gufunc_ir.blocks[l] = b
                    body_last_label = max(loop_body.keys())
                    gufunc_ir.blocks[new_label] = block
                    gufunc_ir.blocks[label] = prev_block
                    # Add a jump from the last parfor body block to the block containing
                    # statements after the sentinel.
                    gufunc_ir.blocks[body_last_label].append(
                        ir.Jump(new_label, loc))
                    break
            else:
                continue
            break

    if config.DEBUG_ARRAY_OPT:
        print("gufunc_ir last dump before renaming")
        gufunc_ir.dump()

    gufunc_ir.blocks = rename_labels(gufunc_ir.blocks)
    remove_dels(gufunc_ir.blocks)

    if config.DEBUG_ARRAY_OPT:
        sys.stdout.flush()

    if config.DEBUG_ARRAY_OPT:
        print("gufunc_ir last dump")
        gufunc_ir.dump()
        print("flags", flags)
        print("typemap", typemap)

    old_alias = flags.noalias
    if not has_aliases:
        if config.DEBUG_ARRAY_OPT:
            print("No aliases found so adding noalias flag.")
        flags.noalias = True

    remove_dead(gufunc_ir.blocks, gufunc_ir.arg_names, gufunc_ir, typemap)

    if config.DEBUG_ARRAY_OPT:
        print("gufunc_ir after remove dead")
        gufunc_ir.dump()

    kernel_sig = signature(types.none, *gufunc_param_types)

    if config.DEBUG_ARRAY_OPT:
        sys.stdout.flush()

    #if target=='spirv':
    kernel_func = numba.dppy.compiler.compile_kernel_parfor(
        driver.runtime.get_gpu_device(),
        gufunc_ir,
        gufunc_param_types,
        param_types_addrspaces)

    flags.noalias = old_alias

    if config.DEBUG_ARRAY_OPT:
        print("kernel_sig = ", kernel_sig)

    return kernel_func, parfor_args, kernel_sig, redargstartdim, func_arg_types




def _lower_parfor_dppy(lowerer, parfor):
    """Lowerer that handles LLVM code generation for parfor.
    This function lowers a parfor IR node to LLVM.
    The general approach is as follows:
    1) The code from the parfor's init block is lowered normally
       in the context of the current function.
    2) The body of the parfor is transformed into a gufunc function.
    3) Code is inserted into the main function that calls do_scheduling
       to divide the iteration space for each thread, allocates
       reduction arrays, calls the gufunc function, and then invokes
       the reduction function across the reduction arrays to produce
       the final reduction values.
    """

    typingctx = lowerer.context.typing_context
    targetctx = lowerer.context
    # We copy the typemap here because for race condition variable we'll
    # update their type to array so they can be updated by the gufunc.
    orig_typemap = lowerer.fndesc.typemap
    # replace original typemap with copy and restore the original at the end.
    lowerer.fndesc.typemap = copy.copy(orig_typemap)
    typemap = lowerer.fndesc.typemap
    varmap = lowerer.varmap

    if config.DEBUG_ARRAY_OPT:
        print("_lower_parfor_parallel")
        parfor.dump()
    if config.DEBUG_ARRAY_OPT:
        sys.stdout.flush()

    loc = parfor.init_block.loc
    scope = parfor.init_block.scope

    # produce instructions for init_block
    if config.DEBUG_ARRAY_OPT:
        print("init_block = ", parfor.init_block, type(parfor.init_block))
    for instr in parfor.init_block.body:
        if config.DEBUG_ARRAY_OPT:
            print("lower init_block instr = ", instr)
        lowerer.lower_inst(instr)

    for racevar in parfor.races:
        if racevar not in varmap:
            rvtyp = typemap[racevar]
            rv = ir.Var(scope, racevar, loc)
            lowerer._alloca_var(rv.name, rvtyp)

    alias_map = {}
    arg_aliases = {}
    numba.parfor.find_potential_aliases_parfor(parfor, parfor.params, typemap,
                                        lowerer.func_ir, alias_map, arg_aliases)
    if config.DEBUG_ARRAY_OPT:
        print("alias_map", alias_map)
        print("arg_aliases", arg_aliases)

    # run get_parfor_outputs() and get_parfor_reductions() before gufunc creation
    # since Jumps are modified so CFG of loop_body dict will become invalid
    assert parfor.params != None

    parfor_output_arrays = numba.parfor.get_parfor_outputs(
        parfor, parfor.params)
    parfor_redvars, parfor_reddict = numba.parfor.get_parfor_reductions(
        lowerer.func_ir, parfor, parfor.params, lowerer.fndesc.calltypes)

    # init reduction array allocation here.
    nredvars = len(parfor_redvars)
    redarrs = {}

    # compile parfor body as a separate function to be used with GUFuncWrapper
    flags = copy.copy(parfor.flags)
    flags.set('error_model', 'numpy')
    # Can't get here unless flags.set('auto_parallel', ParallelOptions(True))
    index_var_typ = typemap[parfor.loop_nests[0].index_variable.name]
    # index variables should have the same type, check rest of indices
    for l in parfor.loop_nests[1:]:
        assert typemap[l.index_variable.name] == index_var_typ
    numba.parfor.sequential_parfor_lowering = True
    loop_ranges = [(l.start, l.stop, l.step) for l in parfor.loop_nests]

    target = 'spirv'

    func, func_args, func_sig, redargstartdim, func_arg_types = _create_gufunc_for_parfor_body(
        lowerer, parfor, target, typemap, typingctx, targetctx, flags, loop_ranges, {},
        bool(alias_map), index_var_typ, parfor.races)
    numba.parfor.sequential_parfor_lowering = False

    # get the shape signature
    get_shape_classes = parfor.get_shape_classes
    num_reductions = len(parfor_redvars)
    num_inputs = len(func_args) - len(parfor_output_arrays) - num_reductions
    if config.DEBUG_ARRAY_OPT:
        print("func", func, type(func))
        print("func_args", func_args, type(func_args))
        print("func_sig", func_sig, type(func_sig))
        print("num_inputs = ", num_inputs)
        print("parfor_outputs = ", parfor_output_arrays)
        print("parfor_redvars = ", parfor_redvars)
        print("num_reductions = ", num_reductions)

    # call the func in parallel by wrapping it with ParallelGUFuncBuilder
    if config.DEBUG_ARRAY_OPT:
        print("loop_nests = ", parfor.loop_nests)
    print("loop_ranges = ", loop_ranges)

    gu_signature = _create_shape_signature(
        parfor.get_shape_classes,
        num_inputs,
        num_reductions,
        func_args,
        redargstartdim,
        func_sig,
        parfor.races,
        typemap)
    call_dppy(
        lowerer,
        func,
        gu_signature,
        func_sig,
        func_args,
        num_inputs,
        func_arg_types,
        loop_ranges,
        parfor_redvars,
        parfor_reddict,
        redarrs,
        parfor.init_block,
        index_var_typ,
        parfor.races)

    if config.DEBUG_ARRAY_OPT:
        sys.stdout.flush()

    if nredvars > 0:
        # Perform the final reduction across the reduction array created above.
        thread_count = get_thread_count()
        scope = parfor.init_block.scope
        loc = parfor.init_block.loc

        # For each reduction variable...
        for i in range(nredvars):
            name = parfor_redvars[i]
            redarr = redarrs[name]
            redvar_typ = lowerer.fndesc.typemap[name]
            if config.DEBUG_ARRAY_OPT:
                print("post-gufunc reduction:", name, redarr, redvar_typ)

            if config.DEBUG_ARRAY_OPT_RUNTIME:
                res_print_str = "res_print"
                strconsttyp = types.StringLiteral(res_print_str)
                lhs = ir.Var(scope, mk_unique_var("str_const"), loc)
                assign_lhs = ir.Assign(value=ir.Const(value=res_print_str, loc=loc),
                                               target=lhs, loc=loc)
                typemap[lhs.name] = strconsttyp
                lowerer.lower_inst(assign_lhs)

                res_print = ir.Print(args=[lhs, redarr], vararg=None, loc=loc)
                lowerer.fndesc.calltypes[res_print] = signature(types.none,
                                                         typemap[lhs.name],
                                                         typemap[redarr.name])
                print("res_print", res_print)
                lowerer.lower_inst(res_print)

            # For each element in the reduction array created above.
            for j in range(get_thread_count()):
                # Create index var to access that element.
                index_var = ir.Var(scope, mk_unique_var("index_var"), loc)
                index_var_assign = ir.Assign(ir.Const(j, loc), index_var, loc)
                typemap[index_var.name] = types.uintp
                lowerer.lower_inst(index_var_assign)

                # Read that element from the array into oneelem.
                oneelem = ir.Var(scope, mk_unique_var("redelem"), loc)
                oneelemgetitem = ir.Expr.getitem(redarr, index_var, loc)
                typemap[oneelem.name] = redvar_typ
                lowerer.fndesc.calltypes[oneelemgetitem] = signature(redvar_typ,
                        typemap[redarr.name], typemap[index_var.name])
                oneelemassign = ir.Assign(oneelemgetitem, oneelem, loc)
                lowerer.lower_inst(oneelemassign)

                init_var = ir.Var(scope, name+"#init", loc)
                init_assign = ir.Assign(oneelem, init_var, loc)
                if name+"#init" not in typemap:
                    typemap[init_var.name] = redvar_typ
                lowerer.lower_inst(init_assign)

                if config.DEBUG_ARRAY_OPT_RUNTIME:
                    res_print_str = "res_print1 for thread " + str(j) + ":"
                    strconsttyp = types.StringLiteral(res_print_str)
                    lhs = ir.Var(scope, mk_unique_var("str_const"), loc)
                    assign_lhs = ir.Assign(value=ir.Const(value=res_print_str, loc=loc),
                                               target=lhs, loc=loc)
                    typemap[lhs.name] = strconsttyp
                    lowerer.lower_inst(assign_lhs)

                    res_print = ir.Print(args=[lhs, index_var, oneelem, init_var, ir.Var(scope, name, loc)],
                                         vararg=None, loc=loc)
                    lowerer.fndesc.calltypes[res_print] = signature(types.none,
                                                             typemap[lhs.name],
                                                             typemap[index_var.name],
                                                             typemap[oneelem.name],
                                                             typemap[init_var.name],
                                                             typemap[name])
                    print("res_print1", res_print)
                    lowerer.lower_inst(res_print)

                # generate code for combining reduction variable with thread output
                for inst in parfor_reddict[name][1]:
                    # If we have a case where a parfor body has an array reduction like A += B
                    # and A and B have different data types then the reduction in the parallel
                    # region will operate on those differeing types.  However, here, after the
                    # parallel region, we are summing across the reduction array and that is
                    # guaranteed to have the same data type so we need to change the reduction
                    # nodes so that the right-hand sides have a type equal to the reduction-type
                    # and therefore the left-hand side.
                    if isinstance(inst, ir.Assign):
                        rhs = inst.value
                        # We probably need to generalize this since it only does substitutions in
                        # inplace_binops.
                        if (isinstance(rhs, ir.Expr) and rhs.op == 'inplace_binop' and
                            rhs.rhs.name == init_var.name):
                            if config.DEBUG_ARRAY_OPT:
                                print("Adding call to reduction", rhs)
                            if rhs.fn == operator.isub:
                                rhs.fn = operator.iadd
                                rhs.immutable_fn = operator.add
                            if rhs.fn == operator.itruediv or rhs.fn == operator.ifloordiv:
                                rhs.fn = operator.imul
                                rhs.immutable_fn = operator.mul
                            if config.DEBUG_ARRAY_OPT:
                                print("After changing sub to add or div to mul", rhs)
                            # Get calltype of rhs.
                            ct = lowerer.fndesc.calltypes[rhs]
                            assert(len(ct.args) == 2)
                            # Create new arg types replace the second arg type with the reduction var type.
                            ctargs = (ct.args[0], redvar_typ)
                            # Update the signature of the call.
                            ct = ct.replace(args=ctargs)
                            # Remove so we can re-insert since calltypes is unique dict.
                            lowerer.fndesc.calltypes.pop(rhs)
                            # Add calltype back in for the expr with updated signature.
                            lowerer.fndesc.calltypes[rhs] = ct
                    lowerer.lower_inst(inst)
                    if isinstance(inst, ir.Assign) and name == inst.target.name:
                        break

                    if config.DEBUG_ARRAY_OPT_RUNTIME:
                        res_print_str = "res_print2 for thread " + str(j) + ":"
                        strconsttyp = types.StringLiteral(res_print_str)
                        lhs = ir.Var(scope, mk_unique_var("str_const"), loc)
                        assign_lhs = ir.Assign(value=ir.Const(value=res_print_str, loc=loc),
                                               target=lhs, loc=loc)
                        typemap[lhs.name] = strconsttyp
                        lowerer.lower_inst(assign_lhs)

                        res_print = ir.Print(args=[lhs, index_var, oneelem, init_var, ir.Var(scope, name, loc)],
                                             vararg=None, loc=loc)
                        lowerer.fndesc.calltypes[res_print] = signature(types.none,
                                                                 typemap[lhs.name],
                                                                 typemap[index_var.name],
                                                                 typemap[oneelem.name],
                                                                 typemap[init_var.name],
                                                                 typemap[name])
                        print("res_print2", res_print)
                        lowerer.lower_inst(res_print)


        # Cleanup reduction variable
        for v in redarrs.values():
            lowerer.lower_inst(ir.Del(v.name, loc=loc))
    # Restore the original typemap of the function that was replaced temporarily at the
    # Beginning of this function.
    lowerer.fndesc.typemap = orig_typemap


def _create_shape_signature(
        get_shape_classes,
        num_inputs,
        num_reductions,
        args,
        redargstartdim,
        func_sig,
        races,
        typemap):
    '''Create shape signature for GUFunc
    '''
    if config.DEBUG_ARRAY_OPT:
        print("_create_shape_signature", num_inputs, num_reductions, args, redargstartdim)
        arg_start_print = 0
        for i in args[arg_start_print:]:
            print("argument", i, type(i), get_shape_classes(i, typemap=typemap))

    num_inouts = len(args) - num_reductions
    # maximum class number for array shapes
    classes = [get_shape_classes(var, typemap=typemap) if var not in races else (-1,) for var in args[1:]]
    class_set = set()
    for _class in classes:
        if _class:
            for i in _class:
                class_set.add(i)
    max_class = max(class_set) + 1 if class_set else 0
    classes.insert(0, (max_class,)) # force set the class of 'sched' argument
    class_set.add(max_class)
    class_map = {}
    # TODO: use prefix + class number instead of single char
    alphabet = ord('a')
    for n in class_set:
       if n >= 0:
           class_map[n] = chr(alphabet)
           alphabet += 1

    alpha_dict = {'latest_alpha' : alphabet}

    def bump_alpha(c, class_map):
        if c >= 0:
            return class_map[c]
        else:
            alpha_dict['latest_alpha'] += 1
            return chr(alpha_dict['latest_alpha'])

    gu_sin = []
    gu_sout = []
    count = 0
    syms_sin = ()
    if config.DEBUG_ARRAY_OPT:
        print("args", args)
        print("classes", classes)
    for cls, arg in zip(classes, args):
        count = count + 1
        if cls:
            dim_syms = tuple(bump_alpha(c, class_map) for c in cls)
        else:
            dim_syms = ()
        if (count > num_inouts):
            # Strip the first symbol corresponding to the number of workers
            # so that guvectorize will parallelize across the reduction.
            gu_sin.append(dim_syms[redargstartdim[arg]:])
        else:
            gu_sin.append(dim_syms)
            syms_sin += dim_syms
    return (gu_sin, gu_sout)


# Keep all the dppy kernels and programs created alive indefinitely.
keep_alive_kernels = []

def call_dppy(lowerer, cres, gu_signature, outer_sig, expr_args, num_inputs, expr_arg_types,
                loop_ranges, redvars, reddict, redarrdict, init_block, index_var_typ, races):
    '''
    Adds the call to the gufunc function from the main function.
    '''
    context = lowerer.context
    builder = lowerer.builder
    sin, sout = gu_signature
    num_dim = len(loop_ranges)

    # Commonly used LLVM types and constants
    byte_t = lc.Type.int(8)
    byte_ptr_t = lc.Type.pointer(byte_t)
    byte_ptr_ptr_t = lc.Type.pointer(byte_ptr_t)
    intp_t = context.get_value_type(types.intp)
    uintp_t = context.get_value_type(types.uintp)
    intp_ptr_t = lc.Type.pointer(intp_t)
    uintp_ptr_t = lc.Type.pointer(uintp_t)
    zero = context.get_constant(types.uintp, 0)
    one = context.get_constant(types.uintp, 1)
    one_type = one.type
    sizeof_intp = context.get_abi_sizeof(intp_t)
    void_ptr_t = context.get_value_type(types.voidptr)
    void_ptr_ptr_t = lc.Type.pointer(void_ptr_t)
    sizeof_void_ptr = context.get_abi_sizeof(intp_t)

    if config.DEBUG_ARRAY_OPT:
        print("call_dppy")
        print("args = ", expr_args)
        print("outer_sig = ", outer_sig.args, outer_sig.return_type,
              outer_sig.recvr, outer_sig.pysig)
        print("loop_ranges = ", loop_ranges)
        print("expr_args", expr_args)
        print("expr_arg_types", expr_arg_types)
        print("gu_signature", gu_signature)
        print("sin", sin)
        print("sout", sout)
        print("cres", cres, type(cres))
#        print("cres.library", cres.library, type(cres.library))
#        print("cres.fndesc", cres.fndesc, type(cres.fndesc))

    # Compute number of args ------------------------------------------------
    num_expanded_args = 0

    for arg_type in expr_arg_types:
        if isinstance(arg_type, types.npytypes.Array):
            num_expanded_args += 5 + (2 * arg_type.ndim)
        else:
            num_expanded_args += 1

    if config.DEBUG_ARRAY_OPT:
        print("num_expanded_args = ", num_expanded_args)
    # -----------------------------------------------------------------------

    # Create functions that we need to call ---------------------------------

    create_dppy_kernel_arg_fnty = lc.Type.function(
        intp_t, [void_ptr_ptr_t, intp_t, void_ptr_ptr_t])
    create_dppy_kernel_arg = builder.module.get_or_insert_function(create_dppy_kernel_arg_fnty,
                                                          name="create_dp_kernel_arg")

    create_dppy_kernel_arg_from_buffer_fnty = lc.Type.function(
        intp_t, [void_ptr_ptr_t, void_ptr_ptr_t])
    create_dppy_kernel_arg_from_buffer = builder.module.get_or_insert_function(
                                               create_dppy_kernel_arg_from_buffer_fnty,
                                               name="create_dp_kernel_arg_from_buffer")

    create_dppy_rw_mem_buffer_fnty = lc.Type.function(
        intp_t, [void_ptr_t, intp_t, void_ptr_ptr_t])
    create_dppy_rw_mem_buffer = builder.module.get_or_insert_function(
                                      create_dppy_rw_mem_buffer_fnty,
                                      name="create_dp_rw_mem_buffer")

    write_mem_buffer_to_device_fnty = lc.Type.function(
        intp_t, [void_ptr_t, void_ptr_t, intp_t, intp_t, intp_t, void_ptr_t])
    write_mem_buffer_to_device = builder.module.get_or_insert_function(
                                      write_mem_buffer_to_device_fnty,
                                      name="write_dp_mem_buffer_to_device")

    read_mem_buffer_from_device_fnty = lc.Type.function(
        intp_t, [void_ptr_t, void_ptr_t, intp_t, intp_t, intp_t, void_ptr_t])
    read_mem_buffer_from_device = builder.module.get_or_insert_function(
                                    read_mem_buffer_from_device_fnty,
                                    name="read_dp_mem_buffer_from_device")

    enqueue_kernel_fnty = lc.Type.function(
        intp_t, [void_ptr_t, void_ptr_t, intp_t, void_ptr_ptr_t,
                 intp_t, intp_ptr_t, intp_ptr_t])
    enqueue_kernel = builder.module.get_or_insert_function(
                                  enqueue_kernel_fnty,
                                  name="set_args_and_enqueue_dp_kernel_auto_blocking")

    kernel_arg_array = cgutils.alloca_once(
        builder, void_ptr_t, size=context.get_constant(
            types.uintp, num_expanded_args), name="kernel_arg_array")

    # -----------------------------------------------------------------------

    # Get the LLVM vars for the Numba IR reduction array vars.
    red_llvm_vars = [lowerer.getvar(redarrdict[x].name) for x in redvars]
    red_val_types = [context.get_value_type(lowerer.fndesc.typemap[redarrdict[x].name]) for x in redvars]
    redarrs = [lowerer.loadvar(redarrdict[x].name) for x in redvars]
    nredvars = len(redvars)
    ninouts = len(expr_args) - nredvars

    def getvar_or_none(lowerer, x):
        try:
            return lowerer.getvar(x)
        except:
            return None

    def loadvar_or_none(lowerer, x):
        try:
            return lowerer.loadvar(x)
        except:
            return None

    def val_type_or_none(context, lowerer, x):
        try:
            return context.get_value_type(lowerer.fndesc.typemap[x])
        except:
            return None

    all_llvm_args = [getvar_or_none(lowerer, x) for x in expr_args[:ninouts]] + red_llvm_vars
    all_val_types = [val_type_or_none(context, lowerer, x) for x in expr_args[:ninouts]] + red_val_types
    all_args = [loadvar_or_none(lowerer, x) for x in expr_args[:ninouts]] + redarrs

    # Create a NULL void * pointer for meminfo and parent parts of ndarrays.
    null_ptr = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="null_ptr")
    builder.store(builder.inttoptr(context.get_constant(types.uintp, 0), void_ptr_t), null_ptr)
    #builder.store(cgutils.get_null_value(byte_ptr_t), null_ptr)

    gpu_device = driver.runtime.get_gpu_device()
    gpu_device_env = gpu_device.get_env_ptr()
    max_work_group_size = gpu_device.get_max_work_group_size()
    gpu_device_int = int(driver.ffi.cast("uintptr_t", gpu_device_env))
    #print("gpu_device_env", gpu_device_env, type(gpu_device_env), gpu_device_int)
    kernel_t_obj = cres.kernel._kernel_t_obj[0]
    kernel_int = int(driver.ffi.cast("uintptr_t", kernel_t_obj))
    #print("kernel_t_obj", kernel_t_obj, type(kernel_t_obj), kernel_int)
    keep_alive_kernels.append(cres)

    # -----------------------------------------------------------------------
    # Call clSetKernelArg for each arg and create arg array for the enqueue function.
    cur_arg = 0
    read_bufs_after_enqueue = []
    # Put each part of each argument into kernel_arg_array.
#    for var, arg, llvm_arg, arg_type, gu_sig, val_type, index in zip(expr_args, all_args, all_llvm_args,
#                                     expr_arg_types, sin + sout, all_val_types, range(len(expr_args))):
    for var, llvm_arg, arg_type, gu_sig, val_type, index in zip(expr_args, all_llvm_args,
                                     expr_arg_types, sin + sout, all_val_types, range(len(expr_args))):
        if config.DEBUG_ARRAY_OPT:
            print("var:", var, type(var),
#                  "\n\targ:", arg, type(arg),
                  "\n\tllvm_arg:", llvm_arg, type(llvm_arg),
                  "\n\targ_type:", arg_type, type(arg_type),
                  "\n\tgu_sig:", gu_sig,
                  "\n\tval_type:", val_type, type(val_type),
                  "\n\tindex:", index)

        if isinstance(arg_type, types.npytypes.Array):
            # --------------------------------------------------------------------------------------
            if llvm_arg is not None:
                # Handle meminfo.  Not used by kernel so just write a null pointer.
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))

                builder.call(
                    create_dppy_kernel_arg, [null_ptr,
                                               context.get_constant(types.uintp, sizeof_void_ptr),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)

                #cgutils.printf(builder, "dst0 = ")
                #cgutils.printf(builder, "%p->%p ", dst, builder.load(kernel_arg))
                #cgutils.printf(builder, "\n")

                # Handle parent.  Not used by kernel so just write a null pointer.
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                builder.call(
                    create_dppy_kernel_arg, [null_ptr,
                                               context.get_constant(types.uintp, sizeof_void_ptr),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)

                #cgutils.printf(builder, "dst1 = ")
                #cgutils.printf(builder, "%p->%p ", dst, builder.load(kernel_arg))
                #cgutils.printf(builder, "\n")

                # Handle array size
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                array_size_member = builder.gep(llvm_arg, [context.get_constant(types.int32, 0), context.get_constant(types.int32, 2)])
                builder.call(
                    create_dppy_kernel_arg, [builder.bitcast(array_size_member, void_ptr_ptr_t),
                                               context.get_constant(types.uintp, sizeof_intp),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)
                # Handle itemsize
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                item_size_member = builder.gep(llvm_arg, [context.get_constant(types.int32, 0), context.get_constant(types.int32, 3)])
                builder.call(
                    create_dppy_kernel_arg, [builder.bitcast(item_size_member, void_ptr_ptr_t),
                                               context.get_constant(types.uintp, sizeof_intp),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)
                # Calculate total buffer size
                total_size = cgutils.alloca_once(builder, intp_t, size=context.get_constant(types.uintp, 1), name="total_size" + str(cur_arg))
                builder.store(builder.sext(builder.mul(builder.load(array_size_member), builder.load(item_size_member)), intp_t), total_size)
                # Handle data
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                data_member = builder.gep(llvm_arg, [context.get_constant(types.int32, 0), context.get_constant(types.int32, 4)])
                buffer_name = "buffer_ptr" + str(cur_arg)
                buffer_ptr = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name=buffer_name)
                # env, buffer_size, buffer_ptr
                builder.call(
                    create_dppy_rw_mem_buffer, [builder.inttoptr(context.get_constant(types.uintp, gpu_device_int), void_ptr_t),
                                                  builder.load(total_size),
                                                  buffer_ptr])

                if index < num_inputs:
                    builder.call(
                        write_mem_buffer_to_device, [builder.inttoptr(context.get_constant(types.uintp, gpu_device_int), void_ptr_t),
                                                     builder.load(buffer_ptr),
                                                     context.get_constant(types.uintp, 1),
                                                     context.get_constant(types.uintp, 0),
                                                     builder.load(total_size),
                                                     builder.bitcast(builder.load(data_member), void_ptr_t)])
                else:
                    read_bufs_after_enqueue.append((buffer_ptr, total_size, data_member))

                builder.call(create_dppy_kernel_arg_from_buffer, [buffer_ptr, kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)
                # Handle shape
                shape_member = builder.gep(llvm_arg, [context.get_constant(types.int32, 0), context.get_constant(types.int32, 5)])
                for this_dim in range(arg_type.ndim):
                    kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                    shape_entry = builder.gep(shape_member, [context.get_constant(types.int32, 0), context.get_constant(types.int32, this_dim)])
                    builder.call(
                        create_dppy_kernel_arg, [builder.bitcast(shape_entry, void_ptr_ptr_t),
                                                   context.get_constant(types.uintp, sizeof_intp),
                                                   kernel_arg])
                    dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                    cur_arg += 1
                    builder.store(builder.load(kernel_arg), dst)
                # Handle strides
                stride_member = builder.gep(llvm_arg, [context.get_constant(types.int32, 0), context.get_constant(types.int32, 6)])
                for this_stride in range(arg_type.ndim):
                    kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                    stride_entry = builder.gep(stride_member, [context.get_constant(types.int32, 0), context.get_constant(types.int32, this_dim)])
                    builder.call(
                        create_dppy_kernel_arg, [builder.bitcast(stride_entry, void_ptr_ptr_t),
                                                   context.get_constant(types.uintp, sizeof_intp),
                                                   kernel_arg])
                    dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                    cur_arg += 1
                    builder.store(builder.load(kernel_arg), dst)
            else:
                # --------------------------------------------------------------------------------------
                # Handle meminfo.  Not used by kernel so just write a null pointer.
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))

                builder.call(
                    create_dppy_kernel_arg, [null_ptr,
                                               context.get_constant(types.uintp, sizeof_void_ptr),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)

                # Handle parent.  Not used by kernel so just write a null pointer.
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                builder.call(
                    create_dppy_kernel_arg, [null_ptr,
                                               context.get_constant(types.uintp, sizeof_void_ptr),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)

                # Handle array size.  Equal to the max number of items in a work group.  We might not need the whole thing.
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                dtype_itemsize = context.get_abi_sizeof(context.get_data_type(arg_type.dtype))
                local_size = max_work_group_size * dtype_itemsize
                #print("dtype:", arg_type.dtype, type(arg_type.dtype), dtype_itemsize, local_size, max_work_group_size)

                builder.call(
                    create_dppy_kernel_arg, [builder.inttoptr(context.get_constant(types.uintp, max_work_group_size), void_ptr_ptr_t),
                                               context.get_constant(types.uintp, sizeof_intp),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)
                # Handle itemsize
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                builder.call(
                    create_dppy_kernel_arg, [builder.inttoptr(context.get_constant(types.uintp, dtype_itemsize), void_ptr_ptr_t),
                                               context.get_constant(types.uintp, sizeof_intp),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)
                # Handle data.  Pass null for local data.
                kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                builder.call(
                    create_dppy_kernel_arg, [null_ptr,
                                               context.get_constant(types.uintp, local_size),
                                               kernel_arg])
                dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                cur_arg += 1
                builder.store(builder.load(kernel_arg), dst)
                # Handle shape
                for this_dim in range(arg_type.ndim):
                    kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                    builder.call(
                        create_dppy_kernel_arg, [builder.inttoptr(context.get_constant(types.uintp, max_work_group_size), void_ptr_ptr_t),
                                                   context.get_constant(types.uintp, sizeof_intp),
                                                   kernel_arg])
                    dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                    cur_arg += 1
                    builder.store(builder.load(kernel_arg), dst)
                # Handle strides
                for this_stride in range(arg_type.ndim):
                    kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
                    builder.call(
                        create_dppy_kernel_arg, [builder.inttoptr(context.get_constant(types.uintp, 1), void_ptr_ptr_t),
                                                   context.get_constant(types.uintp, sizeof_intp),
                                                   kernel_arg])
                    dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
                    cur_arg += 1
                    builder.store(builder.load(kernel_arg), dst)
        else:
            kernel_arg = cgutils.alloca_once(builder, void_ptr_t, size=context.get_constant(types.uintp, 1), name="kernel_arg" + str(cur_arg))
            # Handle non-arrays
            builder.call(
                create_dppy_kernel_arg, [builder.bitcast(llvm_arg, void_ptr_ptr_t),
                                           context.get_constant(types.uintp, context.get_abi_sizeof(val_type)),
                                           kernel_arg])
            dst = builder.gep(kernel_arg_array, [context.get_constant(types.intp, cur_arg)])
            cur_arg += 1
            builder.store(builder.load(kernel_arg), dst)

    # -----------------------------------------------------------------------

    # loadvars for loop_ranges
    def load_range(v):
        if isinstance(v, ir.Var):
            return lowerer.loadvar(v.name)
        else:
            return context.get_constant(types.uintp, v)

    num_dim = len(loop_ranges)
    for i in range(num_dim):
        start, stop, step = loop_ranges[i]
        start = load_range(start)
        stop = load_range(stop)
        assert(step == 1)  # We do not support loop steps other than 1
        step = load_range(step)
        loop_ranges[i] = (start, stop, step)

    # Package dim start and stops for auto-blocking enqueue.
    dim_starts = cgutils.alloca_once(
        builder, uintp_t, size=context.get_constant(
            types.uintp, num_dim), name="dims")
    dim_stops = cgutils.alloca_once(
        builder, uintp_t, size=context.get_constant(
            types.uintp, num_dim), name="dims")
    for i in range(num_dim):
        start, stop, step = loop_ranges[i]
        if start.type != one_type:
            start = builder.sext(start, one_type)
        if stop.type != one_type:
            stop = builder.sext(stop, one_type)
        if step.type != one_type:
            step = builder.sext(step, one_type)
        # substract 1 because do-scheduling takes inclusive ranges
        stop = builder.sub(stop, one)
        builder.store(
            start, builder.gep(
                dim_starts, [
                    context.get_constant(
                        types.uintp, i)]))
        builder.store(stop, builder.gep(dim_stops,
                                        [context.get_constant(types.uintp, i)]))

    builder.call(
        enqueue_kernel, [builder.inttoptr(context.get_constant(types.uintp, gpu_device_int), void_ptr_t),
                         builder.inttoptr(context.get_constant(types.uintp, kernel_int), void_ptr_t),
                         context.get_constant(types.uintp, num_expanded_args),
                         kernel_arg_array,
                         #builder.bitcast(kernel_arg_array, void_ptr_ptr_t),
                         context.get_constant(types.uintp, num_dim),
                         dim_starts,
                         dim_stops])

    for read_buf in read_bufs_after_enqueue:
        buffer_ptr, array_size_member, data_member = read_buf
        print("read_buf:", buffer_ptr, "array_size_member:", array_size_member, "data_member:", data_member)
        builder.call(
            read_mem_buffer_from_device, [builder.inttoptr(context.get_constant(types.uintp, gpu_device_int), void_ptr_t),
                                          builder.load(buffer_ptr),
                                          context.get_constant(types.uintp, 1),
                                          context.get_constant(types.uintp, 0),
                                          builder.load(array_size_member),
                                          builder.bitcast(builder.load(data_member), void_ptr_t)])
                                          #builder.load(data_member)])





from numba.lowering import Lower

class DPPyLower(Lower):
    def __init__(self, context, library, fndesc, func_ir, metadata=None):
        Lower.__init__(self, context, library, fndesc, func_ir, metadata)
        #lowering.lower_extensions[parfor.Parfor] = _lower_parfor_dppy
        lowering.lower_extensions[parfor.Parfor] = _lower_parfor_dppy_no_gufunc

def dppy_lower_array_expr(lowerer, expr):
    raise NotImplementedError(expr)