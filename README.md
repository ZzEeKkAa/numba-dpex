[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Coverage Status](https://coveralls.io/repos/github/IntelPython/numba-dpex/badge.svg?branch=main)](https://coveralls.io/github/IntelPython/numba-dpex?branch=main)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Join the chat at https://matrix.to/#/#Data-Parallel-Python_community:gitter.im](https://badges.gitter.im/Join%20Chat.svg)](https://app.gitter.im/#/room/#Data-Parallel-Python_community:gitter.im)
[![Coverity Scan Build Status](https://scan.coverity.com/projects/29068/badge.svg)](https://scan.coverity.com/projects/intelpython-numba-dpex)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/IntelPython/numba-dpex/badge)](https://securityscorecards.dev/viewer/?uri=github.com/IntelPython/numba-dpex)
<img align="left" src="https://spec.oneapi.io/oneapi-logo-white-scaled.jpg" alt="oneAPI logo" width="75"/>
<br/>
<br/>
<br/>
<br/>



Data Parallel Extension for Numba* (numba-dpex) is an open-source standalone
extension for the [Numba](http://numba.pydata.org) Python JIT compiler.
Numba-dpex provides a [SYCL*](https://sycl.tech/)-like API for kernel
programming Python. SYCL* is an open standard developed by the [Unified
Acceleration Foundation](https://uxlfoundation.org/) as a vendor-agnostic way of
programming different types of data-parallel hardware such as multi-core CPUs,
GPUs, and FPGAs. Numba-dpex's kernel-programming API brings the same programming
model and a similar API to Python. The API allows expressing portable
data-parallel kernels  in Python and then JIT compiling them for different
hardware targets. JIT compilation is supported for hardware that use the
[SPIR-V](https://www.khronos.org/spir/) intermediate representation format that
includes [OpenCL](https://www.khronos.org/opencl/) CPU (Intel, AMD) devices,
OpenCL GPU (Intel integrated and discrete GPUs) devices, and [oneAPI Level
Zero](https://spec.oneapi.io/level-zero/latest/index.html) GPU (Intel integrated
and discrete GPUs) devices.

The kernel programming API does not yet support every SYCL* feature. Refer to
the [SYCL* and numba-dpex feature comparison](https://intelpython.github.io/numba-dpex/latest/supported_sycl_features.html)
page to get a summary of supported features.
Numba-dpex only implements SYCL*'s kernel programming API, all SYCL runtime
Python bindings are provided by the [dpctl](https://github.com/IntelPython/dpctl)
package.

Along with the kernel programming API, numba-dpex extends Numba's
auto-parallelizer to bring device offload capabilities to `prange` loops and
NumPy-like vector expressions. The offload functionality is supported via the
NumPy drop-in replacement library: [dpnp](https://github.com/IntelPython/dpnp).
Note that `dpnp` and NumPy-based expressions can be used together in the same
function, with `dpnp` expressions getting offloaded by `numba-dpex` and NumPy
expressions getting parallelized by Numba.

Refer the [documentation](https://intelpython.github.io/numba-dpex) and examples
to learn more.

# Getting Started

Numba-dpex is part of the Intel&reg; Distribution of Python (IDP) and Intel&reg;
oneAPI AIKit, and can be installed along with oneAPI. Additionally, we support
installing it from Anaconda cloud. Please refer the instructions
on our [documentation page](https://intelpython.github.io/numba-dpex/latest/getting_started.html)
for more details.

Once the package is installed, a good starting point is to run the examples in
the `numba_dpex/examples` directory. The test suite may also be invoked as
follows:

```bash
python -m pytest --pyargs numba_dpex.tests
```

## Conda

To install `numba_dpex` from the Intel(R) channel on Anaconda
cloud, use the following command:

```bash
conda install numba-dpex -c intel -c conda-forge
```

## Pip

The `numba_dpex` can be installed using `pip` obtaining wheel packages either from PyPi or from Intel(R) channel on Anaconda.
To install `numba_dpex` wheel package from Intel(R) channel on Anaconda, run the following command:

```bash
python -m pip install --index-url https://pypi.anaconda.org/intel/simple numba-dpex
```

# Contributing

Please create an issue for feature requests and bug reports. You can also use
the GitHub Discussions feature for general questions.

If you want to chat with the developers, join the
[#Data-Parallel-Python_community](https://app.gitter.im/#/room/#Data-Parallel-Python_community:gitter.im) room on Gitter.im.

Also refer our [CONTRIBUTING](https://github.com/IntelPython/numba-dpex/blob/main/CONTRIBUTING.md) page.

## <a name="commit"></a> Commit Message Guidelines

We have very precise rules over how our git commit messages can be formatted.  This leads to **more
readable messages** that are easy to follow when looking through the **project history**.  But also,
we use the git commit messages to **generate the Angular change log**.

### Commit Message Format
Each commit message consists of a **header**, a **body** and a **footer**.  The header has a special
format that includes a **type**, a **scope** and a **subject**:

```
<type>(<scope>): <subject>
<BLANK LINE>
<body>
<BLANK LINE>
<footer>
```

The **header** is mandatory and the **scope** of the header is optional.

Any line of the commit message cannot be longer 100 characters! This allows the message to be easier
to read on GitHub as well as in various git tools.

The footer should contain a [closing reference to an issue](https://help.github.com/articles/closing-issues-via-commit-messages/) if any.

Samples: (even more [samples](https://github.com/angular/angular/commits/master))

```
docs(changelog): update changelog to beta.5
```
```
fix(release): need to depend on latest rxjs and zone.js

The version in our package.json gets copied to the one we publish, and users need the latest of these.
```

### Revert
If the commit reverts a previous commit, it should begin with `revert: `, followed by the header of the reverted commit. In the body it should say: `This reverts commit <hash>.`, where the hash is the SHA of the commit being reverted.

### Type
Must be one of the following:

* **build**: Changes that affect the build system or external dependencies (example scopes: gulp, broccoli, npm)
* **ci**: Changes to our CI configuration files and scripts (example scopes: Travis, Circle, BrowserStack, SauceLabs)
* **docs**: Documentation only changes
* **feat**: A new feature
* **fix**: A bug fix
* **perf**: A code change that improves performance
* **refactor**: A code change that neither fixes a bug nor adds a feature
* **style**: Changes that do not affect the meaning of the code (white-space, formatting, missing semi-colons, etc)
* **test**: Adding missing tests or correcting existing tests

### Scope
The scope should be the name of the npm package affected (as perceived by the person reading the changelog generated from commit messages.

The following is the list of supported scopes:

* **animations**
* **common**
* **compiler**
* **compiler-cli**
* **core**
* **elements**
* **forms**
* **http**
* **language-service**
* **platform-browser**
* **platform-browser-dynamic**
* **platform-server**
* **platform-webworker**
* **platform-webworker-dynamic**
* **router**
* **service-worker**
* **upgrade**

There are currently a few exceptions to the "use package name" rule:

* **packaging**: used for changes that change the npm package layout in all of our packages, e.g. public path changes, package.json changes done to all packages, d.ts file/format changes, changes to bundles, etc.
* **changelog**: used for updating the release notes in CHANGELOG.md
* **aio**: used for docs-app (angular.io) related changes within the /aio directory of the repo
* none/empty string: useful for `style`, `test` and `refactor` changes that are done across all packages (e.g. `style: add missing semicolons`)

### Subject
The subject contains a succinct description of the change:

* use the imperative, present tense: "change" not "changed" nor "changes"
* don't capitalize the first letter
* no dot (.) at the end

### Body
Just as in the **subject**, use the imperative, present tense: "change" not "changed" nor "changes".
The body should include the motivation for the change and contrast this with previous behavior.

### Footer
The footer should contain any information about **Breaking Changes** and is also the place to
reference GitHub issues that this commit **Closes**.

**Breaking Changes** should start with the word `BREAKING CHANGE:` with a space or two newlines. The rest of the commit message is then used for this.

A detailed explanation can be found in this [document][commit-message-format].
