import re

from torchgen.utils import assert_never

from dataclasses import dataclass
import dataclasses
from typing import List, Dict, Optional, Iterator, Tuple, Set, Sequence, Callable, Union
from enum import Enum, auto
import itertools

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
#
#                           DATA MODEL
#
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
#
# Some general principles for our data model.
#
# - Stop using C++ data types as the internal data representation
#   format.  Instead, the internal data structures are centered
#   around JIT schema representation.  This avoid a big problem
#   with the old codegen where we read in all the types from
#   native_functions.yaml and then immediately had to retranslate
#   them into C++ types.
#
# - More semantic data representation.  Instead of representing
#   everything as dicts and strings, we define dataclasses for
#   every interesting entity the code generation has to deal with.
#   These dataclasses have strong semantic invariants: for example,
#   we generally require them to roundtrip losslessly into the
#   form they were parsed from.  These structures are immutable
#   and you're expected to populate information once during
#   construction.

# Represent a source location; used for better error reporting
@dataclass(frozen=True)
class Location:
    file: str
    line: int

    def __str__(self) -> str:
        return "{}:{}".format(self.file, self.line)


# Valid values of the 'variants' field in native_functions.yaml
Variant = Enum("Variant", ("function", "method"))

# NOTE: Keep the list in sync with `DispatchKey` in c10/core/DispatchKey.h
class DispatchKey(Enum):
    Undefined = 0
    CatchAll = Undefined

    Dense = auto()
    FPGA = auto()
    ORT = auto()
    MPS = auto()
    Vulkan = auto()
    Metal = auto()
    MKLDNN = auto()
    OpenGL = auto()
    OpenCL = auto()
    IDEEP = auto()
    Quantized = auto()
    CustomRNGKeyId = auto()
    MkldnnCPU = auto()
    Sparse = auto()
    SparseCsrCPU = auto()
    SparseCsrCUDA = auto()

    ZeroTensor = auto()
    Meta = auto()
    BackendSelect = auto()
    Named = auto()
    AutogradOther = auto()
    AutogradFunctionality = auto()
    AutogradNestedTensor = auto()
    Tracer = auto()
    Autocast = auto()
    Batched = auto()
    VmapMode = auto()
    TESTING_ONLY_GenericWrapper = auto()
    TESTING_ONLY_GenericMode = auto()
    EndOfFunctionalityKeys = TESTING_ONLY_GenericMode

    CPU = auto()
    CUDA = auto()
    HIP = auto()
    XLA = auto()
    Lazy = auto()
    IPU = auto()
    XPU = auto()
    NestedTensor = auto()
    PrivateUse1 = auto()
    PrivateUse2 = auto()
    PrivateUse3 = auto()

    QuantizedCPU = auto()
    QuantizedCUDA = auto()
    QuantizedXPU = auto()

    SparseCPU = auto()
    SparseCUDA = auto()
    SparseHIP = auto()
    SparseXPU = auto()

    NestedTensorCPU = auto()
    NestedTensorCUDA = auto()

    AutogradCPU = auto()
    AutogradCUDA = auto()
    AutogradXLA = auto()
    AutogradLazy = auto()
    AutogradIPU = auto()
    AutogradMPS = auto()
    AutogradXPU = auto()
    AutogradPrivateUse1 = auto()
    AutogradPrivateUse2 = auto()
    AutogradPrivateUse3 = auto()

    Autograd = auto()
    CompositeImplicitAutograd = auto()
    CompositeExplicitAutograd = auto()
    EndOfAliasKeys = CompositeExplicitAutograd

    CPUTensorId = CPU
    CUDATensorId = CUDA
    PrivateUse1_PreAutograd = AutogradPrivateUse1
    PrivateUse2_PreAutograd = AutogradPrivateUse2
    PrivateUse3_PreAutograd = AutogradPrivateUse3

    def __str__(self) -> str:
        return self.name

    def lower(self) -> str:
        return str(self).lower()

    @staticmethod
    def parse(value: str) -> "DispatchKey":
        for k, v in DispatchKey.__members__.items():
            if k == value:
                return v
        raise AssertionError(f"unknown dispatch key {value}")


STRUCTURED_DISPATCH_KEYS = {DispatchKey.MPS, DispatchKey.CUDA, DispatchKey.CPU}
UFUNC_DISPATCH_KEYS = {DispatchKey.CUDA, DispatchKey.CPU}

# Set of supported dispatch keys
dispatch_keys = [
    DispatchKey.CPU,
    DispatchKey.SparseCPU,
    DispatchKey.SparseCsrCPU,
    DispatchKey.MkldnnCPU,
    DispatchKey.CUDA,
    DispatchKey.MPS,
    DispatchKey.SparseCUDA,
    DispatchKey.SparseCsrCUDA,
    DispatchKey.QuantizedCPU,
    DispatchKey.QuantizedCUDA,
    DispatchKey.CompositeImplicitAutograd,
    DispatchKey.CompositeExplicitAutograd,
    DispatchKey.NestedTensorCPU,
    DispatchKey.NestedTensorCUDA,
    # Meta is a magic key: it is automatically generated for structured
    # kernels
    DispatchKey.Meta,
    DispatchKey.ZeroTensor,
]

# Dispatch keys that "support all backends".  These codegen slightly differently
# then backend specific keys.
def is_generic_dispatch_key(dk: DispatchKey) -> bool:
    return dk in {
        DispatchKey.CompositeExplicitAutograd,
        DispatchKey.CompositeImplicitAutograd,
    }


# CUDA specific dispatch keys
def is_cuda_dispatch_key(dk: DispatchKey) -> bool:
    return dk in {
        DispatchKey.CUDA,
        DispatchKey.QuantizedCUDA,
        DispatchKey.SparseCUDA,
        DispatchKey.SparseCsrCUDA,
        DispatchKey.NestedTensorCUDA,
        DispatchKey.AutogradCUDA,
        DispatchKey.CUDATensorId,
    }


# Structured kernel generation is only supported for certain key types;
# otherwise use old-style
def is_structured_dispatch_key(dk: DispatchKey) -> bool:
    return dk in STRUCTURED_DISPATCH_KEYS


def is_ufunc_dispatch_key(dk: DispatchKey) -> bool:
    # For now, ufunc dispatch keys coincide with structured keys
    return dk in UFUNC_DISPATCH_KEYS


# This is oddly named ScalarType and not DType for symmetry with C++
class ScalarType(Enum):
    Byte = auto()
    Char = auto()
    Short = auto()
    Int = auto()
    Long = auto()
    Half = auto()
    Float = auto()
    Double = auto()
    ComplexHalf = auto()
    ComplexFloat = auto()
    ComplexDouble = auto()
    Bool = auto()
    BFloat16 = auto()

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def maybe_parse(value: str) -> Optional["ScalarType"]:
        for k, v in ScalarType.__members__.items():
            if k == value:
                return v
        return None

    @staticmethod
    def parse(value: str) -> "ScalarType":
        mb_r = ScalarType.maybe_parse(value)
        assert mb_r is not None, f"unknown dtype {value}"
        return mb_r

    @staticmethod
    def parse_set(values: str) -> Set["ScalarType"]:
        dtypes: Set[ScalarType] = set()
        for value in values.split(", "):
            if value in DTYPE_CLASSES:
                dtypes.update(DTYPE_CLASSES[value])
            else:
                dtypes.add(ScalarType.parse(value))
        return dtypes


DTYPE_CLASSES: Dict[str, Set[ScalarType]] = {}
# NB: Integral doesn't include boolean
DTYPE_CLASSES["Integral"] = {
    ScalarType.Byte,
    ScalarType.Char,
    ScalarType.Int,
    ScalarType.Long,
    ScalarType.Short,
}
# NB: Floating doesn't include low precision types
DTYPE_CLASSES["Floating"] = {ScalarType.Float, ScalarType.Double}
DTYPE_CLASSES["Complex"] = {ScalarType.ComplexFloat, ScalarType.ComplexDouble}
DTYPE_CLASSES["All"] = DTYPE_CLASSES["Integral"] | DTYPE_CLASSES["Floating"]
DTYPE_CLASSES["AllAndComplex"] = DTYPE_CLASSES["All"] | DTYPE_CLASSES["Complex"]
DTYPE_CLASSES["FloatingAndComplex"] = (
    DTYPE_CLASSES["Floating"] | DTYPE_CLASSES["Complex"]
)


# Represents the valid entries for ufunc_inner_loop in native_functions.yaml.
# NB: if you add a new UfuncKey, you will teach torchgen.dest.ufunc how
# to process it.  Most logic will ignore keys they don't understand, so your
# new key will get silently ignored until you hook in logic to deal with it.
class UfuncKey(Enum):
    # These are low level keys that represent exactly one particular
    # instantiation of the kernel produced by codegen
    CUDAFunctor = auto()
    CUDAFunctorOnOther = auto()
    CUDAFunctorOnSelf = auto()

    CPUScalar = auto()
    CPUVector = auto()

    # These are the ones users will usually specify, and
    # implicitly "fill in" the low level keys
    ScalarOnly = auto()  # CUDA*, CPUScalar
    Generic = auto()  # CUDA*, CPU*

    def __str__(self) -> str:
        return self.name

    @staticmethod
    def parse(value: str) -> "UfuncKey":
        for k, v in UfuncKey.__members__.items():
            if k == value:
                return v
        raise AssertionError(f"unknown ufunc key {value}")


class DeviceCheckType(Enum):
    NoCheck = 0
    ExactSame = 1


ViewSchemaKind = Enum(
    "ViewSchemaKind", ("aliasing", "aliasing_inplace", "non_aliasing")
)

# The basic input to the code generation is native_functions.yaml.
# The name "native", BTW, comes from the distinction between native
# functions and legacy TH functions.  The legacy TH functions are gone,
# but the "native" descriptor has stuck.
#
# NativeFunction models a single entry in native_functions.yaml.  Its
# fields roughly correspond to what you would see in the YAML itself,
# but after canonicalization and parsing has occurred.
#
# You can see some of the overall design patterns for how we setup
# dataclasses in this class, but we will defer a complete discussion
# of this at FunctionSchema.
@dataclass(frozen=True)
class NativeFunction:
    # The function schema of the operator in question.  This schema
    # has been parsed; see FunctionSchema for more about its structure.
    # (This type is quoted as we are forward referencing a type
    # defined later in the file.  I opted for this ordering of the
    # classes for expository clarity.)
    func: "FunctionSchema"

    # Whether or not to generate mutable tensor arguments like regular
    # ones
    use_const_ref_for_mutable_tensors: bool

    # Whether or not to omit automatic generation of a DeviceGuard
    device_guard: bool

    # How to emit automatic generation of device check
    device_check: DeviceCheckType

    # What python module to put the function in
    python_module: Optional[str]

    # TODO: figure out what this does
    category_override: Optional[str]

    # If no variants are specified in native_functions.yaml, this is
    # assumed to be {'function'}.
    variants: Set[Variant]

    # Whether or not we should skip generating registrations for
    # this kernel.  This is a bit of a double-edged sword, as manual
    # registrations don't participate in codegen-based selective build!
    manual_kernel_registration: bool

    # Whether or not to skip generating TensorMethod/Functions bindings
    # for this kernel.  Technically, this doesn't actually skip generating
    # the binding; instead, the binding gets generated to __dispatch_{funcname}
    # so you can make use of the normal binding if you need it.
    manual_cpp_binding: bool

    # The location in the YAML file were this native function entry was
    # defined.  This is for conveniently reporting error messages!
    loc: "Location"

    # A list of operators that are expected to be auto-generated for this NativeFunction.
    # Note: This list isn't actually directly used by the codegen to generate anything.
    # Instead, the codegen figures out what operators to generate purely based off of
    # function schema, and uses the autogen declarations to error check.
    # We expect every NativeFunction that gets auto-generated be explicitly called out
    # in native_functions.yaml
    autogen: List["OperatorName"]

    # If non-empty, this kernel is subject to ufunc codegen.
    # Sorted by ufunc_key
    ufunc_inner_loop: Dict[UfuncKey, "UfuncInnerLoop"]

    # Whether or not this out functions is a "structured kernel".  Structured
    # kernels are defined a little differently from normal kernels; in
    # particular, their shape checking logic is defined separately from
    # the kernel.  Only out functions can be structured; other functions
    # delegate to the out function using the structured_delegate keyword.
    # Every structured kernel must have at least an out and a functional
    # variant.
    structured: bool

    # Whether or not this non-out function is a structured kernel, defined
    # in terms of the out kernel referenced by the string here.
    structured_delegate: Optional["OperatorName"]

    # Only valid for structured kernels.  Specifies alternative of what
    # to inherit from when defining the meta class for the structured
    # operator.  This will usually be TensorIteratorBase.  This also
    # changes the semantics of set_output to call the parent class.
    structured_inherits: Optional[str]

    # Structured kernels can declare elements as "precomputed". These elements
    # are returned by the meta function in one struct and passed to the impl
    # function in lieu of certain kernel arguments that these precomputed
    # elements supersede. Information about the names and types of these
    # precomputed elements and how they correspond to kernel arguments is stored
    # in this member, if applicable.
    precomputed: Optional["Precompute"]

    # Argument names whose default  should be excluded from the C++ interface.
    # Intended for resolving overload ambiguities between signatures.
    cpp_no_default_args: Set[str]

    # Note [Abstract ATen methods]
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    # An abstract ATen method is one whose dispatch differs between
    # types.  These are implemented in derived types (with a
    # standard (throwing) definition in Type).  A concrete ATen
    # method is one which has the same dispatch for all types;
    # we just implement it in the base Type.  This is exposed
    # in Declarations.yaml via a field named 'abstract'.
    is_abstract: bool

    # Whether or not the NativeFunction contains a backend-agnostic kernel
    has_composite_implicit_autograd_kernel: bool
    has_composite_explicit_autograd_kernel: bool

    # Tags are used to describe semantic information about (groups of) operators,
    # That aren't easily inferrable directly from the operator's schema.
    tags: Set[str]

    # NB: The benefit of defining a dataclass is that we automatically get
    # a constructor defined for all the fields we specify.  No need
    # to explicitly write it out.

    # We parse both the NativeFunction + backend-specific information about it, which it stored in a corresponding BackendIndex.
    @staticmethod
    def from_yaml(
        ei: Dict[str, object],
        loc: "Location",
        valid_tags: Set[str],
        ignore_keys: Optional[Set[DispatchKey]] = None,
    ) -> Tuple[
        "NativeFunction", Dict[DispatchKey, Dict["OperatorName", "BackendMetadata"]]
    ]:
        """
        Parse a NativeFunction from a dictionary as directly parsed
        from native_functions.yaml
        """
        e = ei.copy()

        funcs = e.pop("func")
        assert isinstance(funcs, str), f"not a str: {funcs}"
        func = FunctionSchema.parse(funcs)

        cpp_no_default_args_list = e.pop("cpp_no_default_args", [])
        assert isinstance(cpp_no_default_args_list, list)
        cpp_no_default_args = set(cpp_no_default_args_list)

        use_const_ref_for_mutable_tensors = e.pop(
            "use_const_ref_for_mutable_tensors", False
        )
        assert isinstance(use_const_ref_for_mutable_tensors, bool)

        variants_s = e.pop("variants", "function")
        assert isinstance(variants_s, str)
        variants: Set[Variant] = set()
        for v in variants_s.split(", "):
            if v == "function":
                variants.add(Variant.function)
            elif v == "method":
                variants.add(Variant.method)
            else:
                raise AssertionError(f"illegal variant {v}")

        manual_kernel_registration = e.pop("manual_kernel_registration", False)
        assert isinstance(
            manual_kernel_registration, bool
        ), f"not a bool: {manual_kernel_registration}"

        manual_cpp_binding = e.pop("manual_cpp_binding", False)
        assert isinstance(manual_cpp_binding, bool), f"not a bool: {manual_cpp_binding}"

        device_guard = e.pop("device_guard", True)
        assert isinstance(device_guard, bool), f"not a bool: {device_guard}"

        device_check_s = e.pop("device_check", None)
        assert device_check_s is None or isinstance(
            device_check_s, str
        ), f"not a str: {device_check_s}"
        device_check: DeviceCheckType
        if device_check_s is None:
            device_check = DeviceCheckType.ExactSame
        else:
            device_check = DeviceCheckType[device_check_s]

        structured = e.pop("structured", False)
        assert isinstance(structured, bool), f"not a bool: {structured}"

        structured_delegate_s = e.pop("structured_delegate", None)
        assert structured_delegate_s is None or isinstance(
            structured_delegate_s, str
        ), f"not a str: {structured_delegate}"
        structured_delegate: Optional[OperatorName] = None
        if structured_delegate_s is not None:
            structured_delegate = OperatorName.parse(structured_delegate_s)

        structured_inherits = e.pop("structured_inherits", None)
        assert structured_inherits is None or isinstance(
            structured_inherits, str
        ), f"not a str: {structured_inherits}"

        python_module = e.pop("python_module", None)
        assert python_module is None or isinstance(
            python_module, str
        ), f"not a str: {python_module}"
        assert (
            python_module is None or Variant.method not in variants
        ), "functions in modules cannot be methods"

        category_override = e.pop("category_override", None)
        assert category_override is None or isinstance(
            category_override, str
        ), f"not a str: {category_override}"

        precomputed_dict = e.pop("precomputed", None)
        assert precomputed_dict is None or structured is True
        precomputed = Precompute.parse(precomputed_dict) if precomputed_dict else None

        tags_s = e.pop("tags", "")
        assert isinstance(tags_s, str)
        tags: Set[str] = set()
        if len(tags_s) > 0:
            assert len(valid_tags) > 0
            for t in tags_s.split(", "):
                # TODO: verify that the tag is valid and has an entry in tags.yaml
                if t in valid_tags:
                    tags.add(t)
                else:
                    raise AssertionError(f"illegal tag {t}")
        assert isinstance(tags, set)

        from torchgen.api import cpp

        raw_dispatch = e.pop("dispatch", None)
        assert raw_dispatch is None or isinstance(raw_dispatch, dict), e
        dispatch: Dict[DispatchKey, BackendMetadata] = {}
        if raw_dispatch is not None:
            assert not manual_kernel_registration, (
                "cannot specify both manual_kernel_registration and dispatch; with "
                "manual registration, dispatch has no effect!"
            )
            redundant_composite_implicit_autograd = False
            for ks, v in raw_dispatch.items():
                if ks == "__line__":
                    continue  # not worth tracking line numbers for dispatch entries
                assert isinstance(ks, str), e
                for k in ks.split(","):
                    dispatch_key = DispatchKey.parse(k.strip())
                    if ignore_keys and dispatch_key in ignore_keys:
                        continue
                    assert dispatch_key in dispatch_keys, (
                        f"Dispatch key {dispatch_key} of kernel {v} "
                        "is not a supported dispatch key."
                    )
                    # Why is 'structured' included? External backends (e.g.
                    # XLA) opt into which ops are structured independently
                    # of which in-tree ops are structured
                    dispatch[dispatch_key] = BackendMetadata(
                        v,
                        structured=structured
                        and is_structured_dispatch_key(dispatch_key),
                    )
                    if (
                        dispatch_key is DispatchKey.CompositeImplicitAutograd
                        and v == cpp.name(func)
                    ):
                        redundant_composite_implicit_autograd = True

            assert not (len(dispatch) == 1 and redundant_composite_implicit_autograd), (
                "unnecessary dispatch table for this function; just delete the dispatch "
                "key entirely"
            )
            # if a function is a structured delegate, deleting the dispatch
            # table is NOT semantics preserving
            assert structured_delegate or dispatch.keys() != {
                DispatchKey.CompositeImplicitAutograd
            }, (
                f"unexpected name for singleton CompositeImplicitAutograd dispatch entry: expected {cpp.name(func)} "
                f"but got {dispatch[DispatchKey.CompositeImplicitAutograd]}.  Rename your implementation to the expected "
                "name, then delete the dispatch table"
            )
        elif not structured and structured_delegate is None:
            dispatch[DispatchKey.CompositeImplicitAutograd] = BackendMetadata(
                cpp.name(func), structured=False
            )

        assert not (
            DispatchKey.CompositeExplicitAutograd in dispatch
            and DispatchKey.CompositeImplicitAutograd in dispatch
        ), (
            "cannot specify both CompositeExplicitAutograd and CompositeImplicitAutograd on a single kernel; each "
            "strictly subsumes the other.  If you wanted to provide an explicit autograd "
            "implementation, specify CompositeExplicitAutograd; otherwise specify CompositeImplicitAutograd only"
        )

        autogen_str = e.pop("autogen", "")
        assert isinstance(autogen_str, str)
        autogen = (
            []
            if autogen_str == ""
            else [OperatorName.parse(x) for x in autogen_str.split(", ")]
        )

        raw_ufunc_inner_loop = e.pop("ufunc_inner_loop", {})
        ufunc_inner_loop = {}
        if isinstance(raw_ufunc_inner_loop, str):
            ufunc_inner_loop[UfuncKey.Generic] = UfuncInnerLoop.parse(
                raw_ufunc_inner_loop, UfuncKey.Generic
            )
        elif isinstance(raw_ufunc_inner_loop, dict):
            for k, vo in raw_ufunc_inner_loop.items():
                if k == "__line__":
                    continue
                assert isinstance(k, str), f"ufunc_inner_loop key is not a str: {k}"
                assert isinstance(vo, str), f"ufunc_inner_loop value is not a str: {v}"
                ufunc_key = UfuncKey.parse(k)
                ufunc_inner_loop[ufunc_key] = UfuncInnerLoop.parse(vo, ufunc_key)
        else:
            raise AssertionError(
                f"ufunc_inner_loop not str or dict: {raw_ufunc_inner_loop}"
            )
        # Program the BackendIndex for the implicit dispatch entry from ufunc
        if ufunc_inner_loop:
            assert structured, "ufunc must be structured"
            for dispatch_key in UFUNC_DISPATCH_KEYS:
                assert (
                    dispatch_key not in dispatch
                ), f"ufunc should not have explicit dispatch entry for {dispatch_key}"
                dispatch[dispatch_key] = BackendMetadata(
                    kernel=ufunc.schema_kernel_name(func, dispatch_key), structured=True
                )

        if structured_delegate:
            # Structured functions MUST have a dispatch table
            is_abstract = True
        else:
            is_abstract = dispatch.keys() != {DispatchKey.CompositeImplicitAutograd}

        has_composite_implicit_autograd_kernel = (
            DispatchKey.CompositeImplicitAutograd in dispatch.keys()
        )
        has_composite_explicit_autograd_kernel = (
            DispatchKey.CompositeExplicitAutograd in dispatch.keys()
        )

        # We aren't going to store dispatch metadata inline in NativeFunctions;
        # instead it is separately indexed by backend (so other backends can
        # add more dispatch entries after the fact).  Reindex the individual
        # metadata by OperatorName!
        backend_metadata = {k: {func.name: v} for k, v in dispatch.items()}

        # don't care if it exists or not; make it easier to use this function
        # with other yaml parsers that aren't setting __line__ in the dict
        e.pop("__line__", None)
        assert not e, f"leftover entries: {e}"

        # Asserts that we can't do in post_init, because they rely on backend-specific info
        if structured_delegate is not None:
            for key in STRUCTURED_DISPATCH_KEYS:
                assert key not in dispatch, (
                    f"if structured_delegate, then must not have {key} in dispatch dictionary "
                    "(it is delegated!)"
                )

        return (
            NativeFunction(
                func=func,
                use_const_ref_for_mutable_tensors=use_const_ref_for_mutable_tensors,
                variants=variants,
                structured=structured,
                structured_delegate=structured_delegate,
                structured_inherits=structured_inherits,
                precomputed=precomputed,
                autogen=autogen,
                ufunc_inner_loop=ufunc_inner_loop,
                manual_kernel_registration=manual_kernel_registration,
                manual_cpp_binding=manual_cpp_binding,
                python_module=python_module,
                category_override=category_override,
                device_guard=device_guard,
                device_check=device_check,
                loc=loc,
                cpp_no_default_args=cpp_no_default_args,
                is_abstract=is_abstract,
                has_composite_implicit_autograd_kernel=has_composite_implicit_autograd_kernel,
                has_composite_explicit_autograd_kernel=has_composite_explicit_autograd_kernel,
                tags=tags,
            ),
            backend_metadata,
        )

    def validate_unstructured(self) -> None:
        # TODO: probably better to accumulate these errors and report them all
        # at once
        assert not self.structured, (
            "This function is structured, but there was "
            "no valid functional variant of it."
        )
        assert self.structured_delegate, (
            "This function delegates to another structured out function, "
            "but no valid function was found (the delegate may not exist, or it has the wrong type)"
        )

    # __post_init__ functions in dataclasses can be used to do extra
    # validation after construction.
    #
    # Notice that we don't do any type validation here.  In fact, we
    # rely exclusively on mypy to check if you've done types correctly!
    # Validation is for nontrivial invariants that cannot be (conveniently)
    # encoded in the type system.
    def __post_init__(self) -> None:
        if self.func.arguments.out:
            assert self.variants == {Variant.function}, (
                "Native functions with out arguments MUST "
                "be declared with only function variant; e.g., variants: function; "
                "otherwise you will tickle a Python argument binding bug "
                "(which usually manifests itself as the result variable being undefined.)"
            )
        if self.structured:
            assert self.func.kind() == SchemaKind.out, (
                "Put structured field on the out= "
                "variant of a function; did you mean structured_delegate?"
            )
            assert (
                self.device_guard
            ), "device_guard: False is not respected by structured kernels"
        if self.structured_delegate:
            assert self.func.kind() != SchemaKind.out, (
                "structured_delegate field not allowed "
                "on out= functions; did you mean structured?"
            )
            assert (
                self.device_guard
            ), "device_guard: False is not respected by structured kernels"
        # Technically, with the asserts above, this assert is impossible to
        # happen
        assert not (
            self.structured and self.structured_delegate
        ), "Cannot have both structured and structured_delegate on function"
        defaulted_arguments = {
            a.name for a in self.func.schema_order_arguments() if a.default is not None
        }
        invalid_args = set.difference(self.cpp_no_default_args, defaulted_arguments)
        assert len(invalid_args) == 0, f"Invalid cpp_no_default_args: {invalid_args}"
        if self.structured_inherits is not None:
            assert (
                self.structured
            ), "structured_inherits must also imply structured: True"
        if str(self.func.name).startswith("_foreach"):
            assert self.device_check == DeviceCheckType.NoCheck, (
                "foreach kernels fall back to slow path when tensor are on different devices, "
                "device_check not allowed to be enabled"
            )

    @property
    def has_composite_kernel(self) -> bool:
        return (
            self.has_composite_implicit_autograd_kernel
            or self.has_composite_explicit_autograd_kernel
        )

    @property
    def is_view_op(self) -> bool:
        rets = self.func.returns
        is_non_mutating_view = len(rets) > 0 and any(
            r.annotation is not None and not r.annotation.is_write for r in rets
        )
        is_inplace_view = "inplace_view" in self.tags
        is_wildcard_view = any(
            inp.annotation is not None and inp.annotation.alias_set_after != ""
            for inp in self.func.schema_order_arguments()
        )
        return is_non_mutating_view or is_inplace_view or is_wildcard_view

    @property
    def view_schema_kind(self) -> ViewSchemaKind:
        if self.is_view_op and self.func.name.name.inplace:
            assert "inplace_view" in self.tags
            return ViewSchemaKind.aliasing_inplace
        if self.is_view_op:
            return ViewSchemaKind.aliasing
        else:
            return ViewSchemaKind.non_aliasing

    @property
    def root_name(self) -> str:
        return self.func.name.name.base


SchemaKind = Enum("SchemaKind", ("functional", "inplace", "out", "mutable"))

# A structured kernel is guaranteed to have a functional and out variant, and
# optionally an inplace variant.
#
# NB: we create NativeFunctionsGroup *even if* the function is not
# actually annotated structured.  Test the structured boolean to see if it
# actually is structured or not.
@dataclass(frozen=True)
class NativeFunctionsGroup:
    functional: NativeFunction
    inplace: Optional[NativeFunction]
    mutable: Optional[NativeFunction]
    out: NativeFunction

    @property
    def structured(self) -> bool:
        # Whether or not the operator has a meta() function. This information is backend-agnostic.
        return self.out.structured

    def __post_init__(self) -> None:
        test_sig: FunctionSchema = self.functional.func.signature()
        for f in self.functions():
            if test_sig != f.func.signature():
                raise AssertionError(
                    "NativeFunctionsGroup constructed from two NativeFunctions "
                    f"that don't have matching signatures: {test_sig} != {f.func.signature()}"
                )
        assert self.functional.func.kind() == SchemaKind.functional
        assert self.out.func.kind() == SchemaKind.out

        if self.inplace is not None:
            assert self.inplace.func.kind() == SchemaKind.inplace

        if self.mutable is not None:
            assert self.mutable.func.kind() == SchemaKind.mutable

        if self.structured:
            # For now, structured composite kernels are not supported (need some
            # design work to figure out how to make the composite case work)
            assert not self.out.has_composite_implicit_autograd_kernel

            assert self.functional.structured_delegate == self.out.func.name, (
                f"{self.functional.func.name} delegates to {self.functional.structured_delegate} "
                f"but its actual delegate is {self.out.func.name}"
            )
            if self.inplace is not None:
                assert self.inplace.structured_delegate == self.out.func.name

        generated_fns = [
            str(f.func.name) for f in self.functions() if "generated" in f.tags
        ]
        generated_fns_str = ", ".join(str(x) for x in generated_fns)
        expected_generated_fns = f.autogen
        expected_generated_fns_str = ", ".join(str(x) for x in expected_generated_fns)
        if len(expected_generated_fns) == 0 and len(generated_fns) > 0:
            raise RuntimeError(
                f"The codegen expects to be able to generate '{generated_fns_str}'."
                " In order to generate them however, we expect them to be called out explicitly in the yaml."
                f" Please add an 'autogen: {generated_fns_str}' line to the entry for {str(f.func.name)}"
            )
        if expected_generated_fns_str != generated_fns_str:
            raise RuntimeError(
                f"The codegen expects to be able to generate '{generated_fns_str}'."
                f" To do so, it expects a line: 'autogen: {generated_fns_str}'."
                f" Instead, it found 'autogen: {generated_fns_str}'"
            )

    def signature(self) -> "FunctionSchema":
        return self.out.func.signature()

    def functions(self) -> Iterator[NativeFunction]:
        yield self.functional
        yield self.out
        if self.inplace is not None:
            yield self.inplace
        if self.mutable is not None:
            yield self.mutable

    @property
    def root_name(self) -> str:
        return self.functional.root_name

    @staticmethod
    def from_dict(
        d: Dict[SchemaKind, NativeFunction]
    ) -> Optional["NativeFunctionsGroup"]:
        assert d
        if len(d) == 1:
            return None
        d = dict(d)  # non-destructive updates please
        functional = d.pop(SchemaKind.functional, None)
        inplace = d.pop(SchemaKind.inplace, None)
        mutable = d.pop(SchemaKind.mutable, None)
        out = d.pop(SchemaKind.out, None)
        assert not d
        assert functional is not None
        # There are a few operators which only have functional/inplace variants;
        # these don't count as structured for our purposes here
        if out is None:
            return None

        return NativeFunctionsGroup(
            functional=functional,
            inplace=inplace,
            mutable=mutable,
            out=out,
        )


@dataclass(frozen=True)
class BackendMetadata:
    # The name of the backend kernel, for a given operator
    # for in-tree backends. These names come directly from the 'dispatch" field
    # in native_functions.yaml. The dispatch entry is optional; in that
    # case, that is equivalent to having written:
    #
    #   dispatch:
    #       CompositeImplicitAutograd: $operator_name
    kernel: str
    # Whether or not the operator has a structured kernel implemented, for this particular backend.
    # For in-tree backends, they all have the same value for structured- this is listed
    # in native_functions.yaml.
    # However, external backends like XLA can indendently toggle which ops are structured.
    structured: bool


@dataclass(frozen=True)
class UfuncInnerLoop:
    name: str
    supported_dtypes: Set[ScalarType]
    # key is stored here because it affects the semantics of name,
    # so its helpful to have them together for further processing
    ufunc_key: UfuncKey

    @staticmethod
    def parse(value: str, ufunc_key: UfuncKey) -> "UfuncInnerLoop":
        name, supported_dtypes_str = value.split(" ", 1)
        assert supported_dtypes_str[0] == "("
        assert supported_dtypes_str[-1] == ")"
        supported_dtypes = set()
        for k in supported_dtypes_str[1:-1].split(", "):
            supported_dtypes |= ScalarType.parse_set(k)
        return UfuncInnerLoop(
            name=name, supported_dtypes=supported_dtypes, ufunc_key=ufunc_key
        )


# BackendIndex represents a backend.
# The BackendIndex encodes per-operator information that is potentially different
# for each backend. The most obvious example is the name of the kernel
# (the 'dispatch' entry in native_functions.yaml).
# However, there can be other examples of different backends having different information.
# External backends can choose to opt their kernels to be structured independently from in-tree backends,
# which means that this information isn't inherentely tied to a NativeFunction- it's different per backend.
@dataclass(frozen=True)
class BackendIndex:
    dispatch_key: DispatchKey
    # Mainly important for structured kernels, this determines which variant in the operator group is used to implement the others.
    # All in-tree ops use out kernels, while XLA uses functional kernels.
    use_out_as_primary: bool
    # Whether the backend requires a device guard, and device checks.
    # For in-tree backends, this is currently just CUDA/HIP
    # For out-of-tree backends, this is currently just Intel XPU
    device_guard: bool
    # Whether the backend is in-tree (CPU/CUDA) or out-of-tree (XLA)
    external: bool
    # Other backend-specific information that is on a per-operator basis
    index: Dict["OperatorName", BackendMetadata]

    @staticmethod
    def grow_index(
        parent_index: Dict[DispatchKey, Dict["OperatorName", BackendMetadata]],
        child_index: Dict[DispatchKey, Dict["OperatorName", BackendMetadata]],
    ) -> None:
        for k, v in child_index.items():
            for op_name, metadata in v.items():
                assert (
                    op_name not in parent_index[k]
                ), f"duplicate operator {op_name} for dispatch key {k}"
                parent_index[k][op_name] = metadata

    def primary(self, g: NativeFunctionsGroup) -> NativeFunction:
        if self.use_out_as_primary:
            return g.out
        else:
            return g.functional

    def has_kernel(self, g: Union[NativeFunction, NativeFunctionsGroup]) -> bool:
        m = self.get_kernel(g)
        return m is not None

    def get_kernel(
        self, g: Union[NativeFunction, NativeFunctionsGroup]
    ) -> Optional[BackendMetadata]:
        if isinstance(g, NativeFunction):
            f = g
        elif isinstance(g, NativeFunctionsGroup):
            f = self.primary(g)
        else:
            assert_never(f)
        if f.func.name not in self.index:
            return None
        return self.index[f.func.name]

    def native_function_class_name(self) -> Optional[str]:
        if self.external:
            return f"{str(self.dispatch_key)}NativeFunctions"
        else:
            # TODO: This discrepancy isn't required; we could also generated
            # a class for in-tree kernels. It'll just require carefully
            # updating every kernel definition + callsite of every in-tree aten kernel.
            return None


# The function schema is undoubtedly the most important data structure
# in all of the codegen, as it defines the type signature for operators,
# and most of the code generation we do is type directed (e.g., look at
# the types, decide what to do.  Think about how we code generate
# C++ function stubs!)
#
# We will also see in this class the general structure for how we model
# data in this code generation.  A few notable properties to point out
# ahead of time:
#
#   - These dataclasses are a *lossless* representation of the strings
#     they are parsed from.  In fact, we assert that given the
#     information stored in the dataclass, we can exactly reconstruct
#     the string we parsed from (and assert this inside the parse
#     definition).  There are a few reasons for this:
#
#       - If you find that it is difficult to reconstruct the string
#         given a dataclass, that is a clue that you are data
#         representation is wrong.
#
#       - It helps ensure that all relevant information is present
#         in the dataclass, so that downstream users aren't tempted
#         to reparse the original string to get some information
#         that was omitted.
#
#       - It forces you to represent the data in-memory in the same way
#         it is recorded textually, which makes the dataclasses easier
#         to understand for someone who is familiar with the
#         textual format.  (As a tradeoff, it means you have to model
#         the syntax, even when it is inconvenient.  But maybe that means
#         the syntax is bad!)  If you don't understand the internal
#         representation, go look at the printing code to see how
#         it maps onto the surface syntax!
#
#       - It makes it easy to test the parsing code, as parsing code
#         that is inconsistent with the string code will fail early
#         and loudly.  (As a tradeoff, it makes the parsing code a bit
#         brittle (in particular, with trivial whitespace changes you
#         are likely to trigger an assert error).
#
#     In general, try to make the __str__ code as simple as possible
#     (even at the cost of more complex parsing logic.)  Additionally,
#     try to minimize redundancy in data representation.  (Precomputed
#     fields are OK though: they are defined as a simple function on
#     the canonical representation in question.)
#
#   - These dataclasses are all frozen; once constructed their
#     values never change.  This makes it easy to tell where any
#     given data came from: just look to the constructor.  As a
#     tradeoff, you can't easily "decorate" a schema with extra
#     information from a post-facto analysis.  We impose this
#     restriction to make these structures more understandable.
#
@dataclass(frozen=True)
class FunctionSchema:
    # The name of the operator this function schema describes.
    name: "OperatorName"

    arguments: "Arguments"

    # TODO: Need to handle collisions with argument names at some point
    returns: Tuple["Return", ...]

    def schema_order_arguments(self) -> Iterator["Argument"]:
        return itertools.chain(
            self.arguments.flat_positional,
            self.arguments.flat_kwarg_only,
            self.arguments.out,
        )

    decl_re = re.compile(r"(?P<name>[^\(]+)\((?P<args>.*)\) -> (?P<returns>.*)")

    @staticmethod
    def parse(func: str) -> "FunctionSchema":
        # We should probably get a proper parser here
        decls = FunctionSchema.decl_re.findall(func)
        assert len(decls) == 1, f"Invalid function schema: {func}"
        ops, args, return_decl = decls[0]
        name = OperatorName.parse(ops)
        arguments = Arguments.parse(args)
        returns = parse_returns(return_decl)
        r = FunctionSchema(name=name, arguments=arguments, returns=returns)
        assert str(r) == func, f"{str(r)} != {func}"
        return r

    def returns_are_aliased(self) -> bool:
        # We assert earlier that schemas can't have a mix of aliased and non-aliased returns
        return any(
            r
            for r in self.returns
            if r.annotation is not None and r.annotation.is_write
        )

    def __post_init__(self) -> None:
        for arg, ret in zip(self.arguments.out, self.returns):
            assert arg.annotation == ret.annotation, (
                "Out arguments must have matching return Tensor; furthermore, "
                "the ith-argument needs to correspond to the ith return"
            )
        # We also enforce that if you have any mutable, positional args, then they are not returned.
        # This makes it easier to group these functions properly with their functional/out= counterparts.
        for a in self.arguments.post_self_positional_mutable:
            assert not any(
                a.annotation == r.annotation for r in self.returns
            ), f"If you have a schema with mutable positional args, we expect them to not be returned. schema: {str(self)}"
        # Invariant: we expect out arguments to appear as keyword arguments in the schema.
        # This means that all mutable returns should be aliased to a keyword argument
        # (except for "self", which we explicitly don't treat as an out argument because of its use in methods)
        # See Note [is_out_fn]
        out_and_self = list(self.arguments.out) + [
            arg for arg in self.arguments.flat_positional if arg.name == "self"
        ]
        mutable_returns = [
            ret
            for ret in self.returns
            if ret.annotation is not None and ret.annotation.is_write
        ]
        immutable_returns = [
            ret
            for ret in self.returns
            if ret.annotation is None or not ret.annotation.is_write
        ]
        # Some assertions: We don't want any functions with a return type of "-> (Tensor(a!), Tensor)",
        # because:
        # (1) It's more annoying to handle properly
        # (2) It's unnecessary - you can't method-chain on the first (mutated) output because it's part of a tuple.
        # Instead, we expect the (a!) argument to not be returned.
        assert (
            len(mutable_returns) == 0 or len(immutable_returns) == 0
        ), f"NativeFunctions must have either only mutable returns, or only immutable returns. Found: {str(self)}"
        for ret in mutable_returns:
            assert any([ret.annotation == arg.annotation for arg in out_and_self]), (
                'All mutable returns must be aliased either to a keyword argument, or to "self". '
                "Did you forget to mark an out argument as keyword-only?"
            )
        if self.arguments.out:
            # out= ops that return their mutable inputs are only really useful for method chaining.
            # And method chaining is only really useful if the thing you're returning is a plain Tensor.
            # So ideally, we'd enforce that out= ops with a single plain mutable tensor should return the tensor,
            # and all other types of out= op schemas should return void.
            # There are a bunch of existing out= ops that return tuples of tensors though, so we're stuck with allowing that.
            if any(a.type != BaseType(BaseTy.Tensor) for a in self.arguments.out):
                assert (
                    len(self.returns) == 0
                ), "out= ops that accept tensor lists as out arguments "
                "are expected to have no return type (since you can't do method chaining on them)"
            else:
                assert len(self.arguments.out) == len(
                    self.returns
                ), "Must return as many arguments as there are out arguments, or no return at all"

        if self.name.name.inplace:
            self_a = self.arguments.self_arg
            assert (
                self_a
                and self_a.argument.annotation
                and self_a.argument.annotation.is_write
            )
            if self_a.argument.type == BaseType(BaseTy.Tensor):
                # All inplace ops with an ordinary `Tensor self` argument should return self,
                # to allow for method chaining.
                assert (
                    len(self.returns) == 1
                    and self.returns[0].annotation == self_a.argument.annotation
                )
            else:
                # You can't method chain on non-tensor self arguments though (like a List[Tensor])
                # so in all other cases we expect the return type to be none.
                assert len(self.returns) == 0

        if self.arguments.tensor_options is not None:
            assert self.kind() == SchemaKind.functional, (
                "Found an operator that is not functional, but has tensor options arguments."
                "This is not allowed- tensor options arguments are only allowed for factory functions."
                f"schema: {str(self)}"
            )
        if self.is_functional_fn():
            assert self.kind() == SchemaKind.functional, (
                "Found an operator that is not functional, but its overload contains the string 'functional'."
                "This is a special keyword in the codegen, please use a different overload name."
                f"schema: {str(self)}"
            )

    def is_functional_fn(self) -> bool:
        return "functional" in self.name.overload_name

    def is_out_fn(self) -> bool:
        # Note [is_out_fn]
        #
        # out functions are the variants which take an explicit out= argument
        # to populate into.  We need to know if a schema corresponds to an
        # out function for several reasons:
        #
        #   - They codegen differently in C++ API
        #       - codegen to at::add_out rather than at::add
        #       - out argument is moved to front of C++ argument list
        #
        # out functions are DEFINED to be any function with a keyword-only
        # argument that is mutable.  In principle, this could lead to a
        # false positive if you define a function that mutates a
        # kwarg only argument, but this isn't the "true" output of this
        # function.  A more robust definition that would work in this
        # case would also look at:
        #
        #   - The output types.  Out functions take in the arguments
        #     they mutate and then return them again; this is sort
        #     of "definitionally" what makes something an out function.
        #     Historically, we DO check this for consistency.
        #   - Correspondence with pure variant.  An out function
        #     should have a signature equivalent to its pure variant,
        #     but just with extra kwargs for the output elements.  This
        #     is difficult to actually check for and historically
        #     we only do this check in tools/
        return bool(self.arguments.out)

    def kind(self) -> SchemaKind:
        """
        What kind of schema is this?  A functional schema is one
        that returns a newly allocated output; an inplace schema
        modifies the self argument inplace; an out schema writes
        the result into an explicitly provided out argument.
        """
        is_out = bool(self.arguments.out)
        is_inplace = self.name.name.inplace
        is_mutable = any(
            a.annotation is not None and a.annotation.is_write
            for a in self.arguments.post_self_positional
        )
        assert not (is_out and is_inplace)
        # out= and inplace schemas can also have post_self_positional mutable args,
        # but we give precedence to out= and inplace when deciding the schema kind.
        # Tradeoff: we probably don't want to have to teach codegen that looks at inplace ops
        # to also worry about mutable post_self_positional arguments,
        # but it seems like a much bigger lift to classify them has having a new schema kind.
        # The number of ops that fit in this strange category is small enough that
        # we can probably manually write code for them instead of forcing the codegen to handle them.
        if is_inplace:
            return SchemaKind.inplace
        elif is_out:
            return SchemaKind.out
        elif is_mutable:
            return SchemaKind.mutable
        else:
            return SchemaKind.functional

    # For every return:
    # - If the return aliases an input, we return the input name
    # - Otherwise, we return None.
    # If return names were enforced to be consistent with aliasing information, then we wouldn't need this.
    def aliased_return_names(self) -> List[Optional[str]]:
        outs: List[Optional[str]] = []
        for r in self.returns:
            aliased_args = [
                a
                for a in self.arguments.flat_all
                if a.annotation is not None and a.annotation == r.annotation
            ]
            if len(aliased_args) == 0:
                outs.append(None)
            elif len(aliased_args) == 1:
                outs.append(aliased_args[0].name)
            else:
                aliased_names = ", ".join(a.name for a in aliased_args)
                raise AssertionError(
                    f"Found a return ({r.name})that aliases multiple inputs ({aliased_names})"
                )
        return outs

    def signature(
        self,
        *,
        strip_default: bool = False,
        strip_view_copy_name: bool = False,
        keep_return_names: bool = False,
    ) -> "FunctionSchema":
        """
                Certain schemas are 'related', in that they are simply
                inplace/out/functional versions of the same function.  This method
                factors these schemas into the "core" functional signature which
                is equal across all versions.

                Here is what normalization happens to the schema to convert
                it to a signature:
                - The overload name is stripped (name is retained, since
                  it expresses semantic content about what the function does)
                - Inplace is set False
                - Out arguments are stripped
                - Mutable post_self_positional args are converted to returns
                - Mutability annotations are stripped  (this is sound
                  because you cannot overload on mutability annotation)
                - Return names are stripped since they are not overloadable and
                  some variants have return names but some not
                - TensorOptions are dropped
                  because out= variants of factory functions don't include them
                  (and we want to be able to pair up factory functions with their out variants)

                Finally, we want to be able to pair up related "view" and their
                corresponding "view_copy" operators. We do this by optionally
                stripping the trailing "_copy" from the base name.

                Example of a mutable op before and after:

                f.func (Mutable operator):
        _fused_moving_avg_obs_fq_helper(Tensor self, Tensor observer_on, Tensor fake_quant_on, Tensor(a!) running_min, Tensor(b!) running_max, Tensor(c!) scale, Tensor(d!) zero_point, float averaging_const, int quant_min, int quant_max, int ch_axis, bool per_row_fake_quant=False, bool symmetric_quant=False) -> (Tensor output, Tensor mask)  # noqa: B950

                f.func (Corresponding functional operator):
        _fused_moving_avg_obs_fq_helper.functional(Tensor self, Tensor observer_on, Tensor fake_quant_on, Tensor running_min, Tensor running_max, Tensor scale, Tensor zero_point, float averaging_const, int quant_min, int quant_max, int ch_axis, bool per_row_fake_quant=False, bool symmetric_quant=False) -> (Tensor output, Tensor mask, Tensor running_min_out, Tensor running_max_out, Tensor scale_out, Tensor zero_point_out)  # noqa: B950

                f.func.signature() output:
        _fused_moving_avg_obs_fq_helper(Tensor self, Tensor observer_on, Tensor fake_quant_on, Tensor running_min, Tensor running_max, Tensor scale, Tensor zero_point, float averaging_const, int quant_min, int quant_max, int ch_axis, bool per_row_fake_quant=False, bool symmetric_quant=False) -> (Tensor, Tensor, Tensor, Tensor, Tensor, Tensor)  # noqa: B950
        """

        def strip_ret_annotation(r: Return) -> Return:
            return Return(
                name=r.name if keep_return_names else None,
                type=r.type,
                annotation=None,
            )

        base_name = self.name.name.base
        if strip_view_copy_name and base_name.endswith("_copy"):
            base_name = base_name.replace("_copy", "")

        # find mutable inputs that are not originally returned, and convert them to returns
        returns_from_mutable_inputs = tuple(
            # When we're grouping functions we strip the return names,
            # but when we're generating the actual functional variants then we follow
            # a convention for what to name the returns
            Return(
                name=f"{a.name}_out" if keep_return_names else None,
                type=a.type,
                annotation=None,
            )
            for a in itertools.chain(
                # Order is important here (otherwise e.g. inplace with mutable args
                # and out= with mutable args won't have the same signature)
                [self.arguments.self_arg.argument]
                if self.arguments.self_arg is not None
                else [],
                self.arguments.out,
                self.arguments.post_self_positional,
            )
            if a.annotation is not None
            and a.annotation.is_write
            and not any(a.annotation == r.annotation for r in self.returns)
        )
        original_returns = tuple(map(strip_ret_annotation, self.returns))
        # Ordering is important here. We expect the "mutable input" returns to come last.
        returns = original_returns + returns_from_mutable_inputs

        args_sig = self.arguments.signature(strip_default=strip_default)
        # See Note [arange.start_step schema]
        if str(self.name) == "arange.start_step":
            args_sig = Arguments.parse(
                str(args_sig).replace("Scalar step", "Scalar step=1")
            )
        # See Note [bernoulli.p schema]
        if str(self.name) == "bernoulli.p":
            args_sig = Arguments.parse(str(args_sig).replace("float p", "float p=0.5"))

        return FunctionSchema(
            name=OperatorName(
                name=BaseOperatorName(
                    base=base_name,
                    inplace=False,
                    dunder_method=self.name.name.dunder_method,
                ),
                overload_name="",  # stripped
            ),
            arguments=args_sig,
            returns=returns,
        )

    def view_signature(self) -> "FunctionSchema":
        return self.signature(strip_view_copy_name=True)

    def with_name(self, name: "OperatorName") -> "FunctionSchema":
        return FunctionSchema(
            name=name,
            arguments=self.arguments,
            returns=self.returns,
        )

    @property
    def modifies_arguments(self) -> bool:
        return self.kind() in [SchemaKind.inplace, SchemaKind.out, SchemaKind.mutable]

    def __str__(self) -> str:
        all_arguments_str = str(self.arguments)
        if len(self.returns) == 1:
            returns = str(self.returns[0])  # omit parentheses
        else:
            returns = "(" + ", ".join(map(str, self.returns)) + ")"
        return f"{self.name}({all_arguments_str}) -> {returns}"


# Here is the rest of the data model, described more briefly.

# Simplified version for what actually shows up in built-ins.
# Look at alias_info.h for expanded syntax.  If you need the structure,
# you also need to make this structure recursive so it can be lined
# up with the type components too.  For primitives this isn't really
# necessary
@dataclass(frozen=True)
class Annotation:
    # Typically only has one element.  Not actually a set so
    # we can conveniently assume it is canonically ordered
    alias_set: Tuple[str, ...]
    is_write: bool
    alias_set_after: str

    @staticmethod
    def parse(ann: str) -> "Annotation":
        # Only handling afterSet == Wildcard for now
        becomes_wildcard_index = ann.find(" -> *")
        if becomes_wildcard_index != -1:
            after_set = "*"
            # TODO: im not good enough with regexes to ignore -> *
            m = re.match(
                r"^([a-z])(!?)(!?)$",
                ann[:becomes_wildcard_index]
                + ann[becomes_wildcard_index + len(" -> *") :],
            )
        else:
            after_set = ""
            m = re.match(r"^([a-z])(!?)(!?)$", ann)
        assert m is not None, f"unrecognized alias annotation {ann}"
        alias_set = (m.group(1),)
        is_write = m.group(2) == "!"
        r = Annotation(
            alias_set=alias_set, is_write=is_write, alias_set_after=after_set
        )
        assert str(r) == ann, f"{r} != {ann}"
        return r

    def __str__(self) -> str:
        alias_set = "|".join(self.alias_set)
        if self.alias_set_after:
            alias_set = f'{alias_set}{" -> "}{self.alias_set_after}'
        is_write = "!" if self.is_write else ""
        return f"{alias_set}{is_write}"


# The base class for the type system.  This is also loosely modeled
# off of jit_type.h, but we've simplified the hierarchy to focus
# in on the aspects of the type system that matter for code generation
# (for example, there's no SingleElementType subclass anymore).
# You never actually construct a Type; usually it's going to be one
# of the subclasses.  If Python had ADTs this would be one!
@dataclass(frozen=True)
class Type:
    @staticmethod
    def parse(t: str) -> "Type":
        r = Type._parse(t)
        assert str(r) == t, f"{r} != {t}"
        return r

    @staticmethod
    def _parse(t: str) -> "Type":
        m = re.match(r"^(.+)\?$", t)
        if m is not None:
            return OptionalType(Type.parse(m.group(1)))
        m = re.match(r"^(.+)\[([0-9]+)?\]$", t)
        if m is not None:
            size = int(m.group(2)) if m.group(2) is not None else None
            return ListType(elem=Type.parse(m.group(1)), size=size)
        try:
            return BaseType(BaseTy[t])
        except KeyError:
            raise RuntimeError(f"unrecognized type {t}")

    def __str__(self) -> str:
        raise NotImplementedError

    # WARNING: These concepts are not very well-defined.  For example,
    # is "int?" nullable? How about "int?[]".  They are defined
    # so we can conveniently generate legacy Declarations.yaml but
    # really we should probably just remove these at some point

    def is_tensor_like(self) -> bool:
        raise NotImplementedError

    def is_nullable(self) -> bool:
        raise NotImplementedError

    def is_list_like(self) -> Optional["ListType"]:
        raise NotImplementedError


# Base types are simple, atomic types with no further structure
BaseTy = Enum(
    "BaseTy",
    (
        "Generator",
        "ScalarType",
        "Tensor",
        "int",
        "Dimname",
        "float",
        "str",
        "bool",
        "Layout",
        "Device",
        "Scalar",
        "MemoryFormat",
        "QScheme",
        "Storage",
        "Stream",
        "SymInt",
        "ConstQuantizerPtr",  # TODO: rename
    ),
)


@dataclass(frozen=True)
class BaseType(Type):
    name: BaseTy

    def __str__(self) -> str:
        return f"{self.name.name}"

    def is_tensor_like(self) -> bool:
        return self.name == BaseTy.Tensor

    def is_nullable(self) -> bool:
        return False

    def is_list_like(self) -> Optional["ListType"]:
        return None


# Optional types may be specified, or may also be validly given None
@dataclass(frozen=True)
class OptionalType(Type):
    elem: Type

    def __str__(self) -> str:
        return f"{self.elem}?"

    def is_tensor_like(self) -> bool:
        return self.elem.is_tensor_like()

    def is_nullable(self) -> bool:
        return True

    def is_list_like(self) -> Optional["ListType"]:
        return self.elem.is_list_like()


# List types specify that we may have multiples of an element.  We
# also support explicit sizes on list types, but these have
# some nontrivial semantics!  (However, for C++ API purposes, explicit
# sizes are mostly erased from the type system.)
#
# DANGER WILL ROBINSON: C++ elaboration depends on elem type; e.g.,
# int[] elaborates differently than bool[3]!
@dataclass(frozen=True)
class ListType(Type):
    elem: Type
    size: Optional[int]

    def __str__(self) -> str:
        size = f"{self.size}" if self.size else ""
        return f"{self.elem}[{size}]"

    def is_tensor_like(self) -> bool:
        return self.elem.is_tensor_like()

    def is_nullable(self) -> bool:
        return self.elem.is_nullable()

    def is_list_like(self) -> Optional["ListType"]:
        return self


@dataclass(frozen=True)
class Argument:
    # NB: I didn't put kwarg_only as a boolean field here, unlike
    # c10::Argument, so that printing works correctly

    name: str
    type: Type
    default: Optional[str]

    # The semantics of the annotation field are a little strange.
    #
    # Alias annotations parametrize Tensors (since Tensors are the only things
    # that can alias.)  This motivates why I write Tensor(a!)?  (and not, for
    # example, Tensor?(a!)), because the (a!) describes aliasing on the tensor,
    # which may be optional (i.e., the alias annotation should bind first to
    # Tensor, before the optional postfix annotation).
    #
    # However, despite being a property of Tensor, we (and c10::Argument)
    # store the annotation at the top level of the Argument, rather than
    # inside the embedded Tensor type.  In the C++ version of this
    # class, we then go through great lengths to mimic the type
    # structure in the annotation structure so we can correlate
    # annotations with types.
    #
    # Now, it turns out, in all applications in code generation, the
    # structure of annotated types is very simple.  So we just hard
    # code it here.  But if we ever do get anything more complex, this
    # model will have to change!
    annotation: Optional[Annotation]

    @staticmethod
    def parse(arg: str) -> "Argument":
        name: str
        default: Optional[str]
        type_and_annot, name_and_default = arg.rsplit(" ", 1)
        if "=" in name_and_default:
            name, default = name_and_default.split("=")
        else:
            name = name_and_default
            default = None
        # TODO: deduplicate annotation matching with Return
        match = re.match(r"Tensor\((.+)\)(.*)", type_and_annot)
        annotation: Optional[Annotation]
        if match:
            # If you update this, make sure the __str__ still works too
            assert match.group(2) in [
                "",
                "?",
                "[]",
            ], "unrecognized alias analysis form with Tensor"
            type_s = "Tensor" + match.group(2)
            annotation = Annotation.parse(match.group(1))
        else:
            type_s = type_and_annot
            annotation = None
        type = Type.parse(type_s)
        r = Argument(
            name=name,
            type=type,
            default=default,
            annotation=annotation,
        )
        assert str(r) == arg, f"{str(r)} != {arg}"
        return r

    @property
    def is_write(self) -> bool:
        return self.annotation is not None and self.annotation.is_write

    def __str__(self) -> str:
        type = f"{self.type}"
        if self.annotation:
            assert type in ["Tensor", "Tensor?", "Tensor[]"]
            type = type.replace("Tensor", f"Tensor({self.annotation})")
        if self.name is None:
            return type
        else:
            mb_default = ""
            if self.default:
                mb_default = f"={self.default}"
            return f"{type} {self.name}{mb_default}"


@dataclass(frozen=True)
class Return:
    name: Optional[str]
    type: Type
    annotation: Optional[Annotation]

    @staticmethod
    def parse(arg: str) -> "Return":
        name: Optional[str]
        if " " in arg:
            type_and_annot, name = arg.rsplit(" ", 1)
        else:
            type_and_annot = arg
            name = None
        match = re.match(r"Tensor\((.+)\)(.*)", type_and_annot)
        annotation: Optional[Annotation]
        if match:
            # If you update this, make sure the __str__ still works too
            assert match.group(2) in [
                "",
                "?",
                "[]",
            ], "unrecognized alias analysis form with Tensor"
            type_s = "Tensor" + match.group(2)
            annotation = Annotation.parse(match.group(1))
        else:
            type_s = type_and_annot
            annotation = None
        type = Type.parse(type_s)
        r = Return(
            name=name,
            type=type,
            annotation=annotation,
        )
        assert str(r) == arg, f"{str(r)} != {arg}"
        return r

    @property
    def is_write(self) -> bool:
        return self.annotation is not None and self.annotation.is_write

    def __str__(self) -> str:
        type = f"{self.type}"
        if self.annotation:
            assert type in ["Tensor", "Tensor?", "Tensor[]"]
            type = type.replace("Tensor", f"Tensor({self.annotation})")
        if self.name is None:
            return type
        else:
            return f"{type} {self.name}"


# Represents the self argument for functions that may be methods
@dataclass(frozen=True)
class SelfArgument:
    argument: Argument


# Bundle of arguments that represent a TensorOptions.  This is mostly
# relevant for the public C++ API but we bake it into the core data
# model because other APIs often have to interact with it
@dataclass(frozen=True)
class TensorOptionsArguments:
    dtype: Argument
    layout: Argument
    device: Argument
    pin_memory: Argument

    def all(self) -> Sequence[Argument]:
        return [self.dtype, self.layout, self.device, self.pin_memory]


@dataclass(frozen=True)
class Arguments:
    # pre_self_positional is usually empty, but is notably non-empty
    # for where.self, where the condition argument comes before the
    # self argument
    pre_self_positional: Tuple[Argument, ...]
    self_arg: Optional[SelfArgument]
    post_self_positional: Tuple[Argument, ...]

    pre_tensor_options_kwarg_only: Tuple[Argument, ...]
    tensor_options: Optional[TensorOptionsArguments]
    # post_tensor_options is typically memory format, which should be
    # part of tensor options but isn't right now, and is usually
    # placed after the tensor options arguments
    post_tensor_options_kwarg_only: Tuple[Argument, ...]

    # Unlike in the previous codegen, we have factored out 'out' arguments
    # in the canonical representation, removing them from kwarg
    # arguments.  This choice is justified by numerous downstream
    # transformations which treat out arguments specially; additionally,
    # you can see that canonicity is not violated!
    out: Tuple[Argument, ...]  # these are also kwarg-only

    @property
    def flat_non_out(self) -> Sequence[Argument]:
        ret: List[Argument] = []
        ret.extend(self.flat_positional)
        ret.extend(self.flat_kwarg_only)
        return ret

    @property
    def flat_positional(self) -> Sequence[Argument]:
        ret: List[Argument] = []
        ret.extend(self.pre_self_positional)
        if self.self_arg is not None:
            ret.append(self.self_arg.argument)
        ret.extend(self.post_self_positional)
        return ret

    @property
    def post_self_positional_mutable(self) -> Sequence[Argument]:
        return [a for a in self.post_self_positional if a.is_write]

    # NB: doesn't contain out arguments
    @property
    def flat_kwarg_only(self) -> Sequence[Argument]:
        ret: List[Argument] = []
        ret.extend(self.pre_tensor_options_kwarg_only)
        if self.tensor_options is not None:
            ret.extend(self.tensor_options.all())
        ret.extend(self.post_tensor_options_kwarg_only)
        return ret

    @property
    def flat_all(self) -> Sequence[Argument]:
        ret: List[Argument] = []
        ret.extend(self.flat_positional)
        ret.extend(self.flat_kwarg_only)
        ret.extend(self.out)
        return ret

    @property
    def non_out(
        self,
    ) -> Sequence[Union[Argument, SelfArgument, TensorOptionsArguments]]:
        ret: List[Union[Argument, SelfArgument, TensorOptionsArguments]] = []
        ret.extend(self.positional)
        ret.extend(self.kwarg_only)
        return ret

    @property
    def positional(self) -> Sequence[Union[Argument, SelfArgument]]:
        ret: List[Union[Argument, SelfArgument]] = []
        ret.extend(self.pre_self_positional)
        if self.self_arg is not None:
            ret.append(self.self_arg)
        ret.extend(self.post_self_positional)
        return ret

    @property
    def kwarg_only(self) -> Sequence[Union[Argument, TensorOptionsArguments]]:
        ret: List[Union[Argument, TensorOptionsArguments]] = []
        ret.extend(self.pre_tensor_options_kwarg_only)
        if self.tensor_options is not None:
            ret.append(self.tensor_options)
        ret.extend(self.post_tensor_options_kwarg_only)
        return ret

    @property
    def all(self) -> Sequence[Union[Argument, SelfArgument, TensorOptionsArguments]]:
        ret: List[Union[Argument, SelfArgument, TensorOptionsArguments]] = []
        ret.extend(self.positional)
        ret.extend(self.kwarg_only)
        ret.extend(self.out)
        return ret

    def mutable_arg_names(self) -> List[str]:
        return [
            a.name
            for a in self.flat_all
            if a.annotation is not None and a.annotation.is_write
        ]

    def signature(self, *, strip_default: bool = False) -> "Arguments":
        # dataclasses.replace could be used here, but it is less
        # type safe so for now I've opted to type everything out
        def strip_arg_annotation(a: Argument) -> Argument:
            return Argument(
                name=a.name,
                type=a.type,
                default=a.default if not strip_default else None,
                annotation=None,
            )

        return Arguments(
            pre_self_positional=tuple(
                map(strip_arg_annotation, self.pre_self_positional)
            ),
            self_arg=SelfArgument(strip_arg_annotation(self.self_arg.argument))
            if self.self_arg is not None
            else None,
            post_self_positional=tuple(
                map(strip_arg_annotation, self.post_self_positional)
            ),
            # Since TensorOptions are droped, the post_tensor_options_kwargs are
            # converted to pre_tensor_options_kwargs
            pre_tensor_options_kwarg_only=tuple(
                map(strip_arg_annotation, self.pre_tensor_options_kwarg_only)
            )
            + tuple(map(strip_arg_annotation, self.post_tensor_options_kwarg_only)),
            # TensorOptions are dropped in signature,
            # so we can pair factory functions with their out= variants.
            tensor_options=None,
            post_tensor_options_kwarg_only=tuple(),
            # out arguments are dropped in signature
            out=(),
        )

    def remove_self_annotation(self) -> "Arguments":
        assert self.self_arg is not None
        return dataclasses.replace(
            self,
            self_arg=SelfArgument(
                dataclasses.replace(self.self_arg.argument, annotation=None)
            ),
        )

    def with_out_args(self, outs: List[Argument]) -> "Arguments":
        assert len(self.out) == 0
        return dataclasses.replace(
            self,
            out=tuple(outs),
        )

    @staticmethod
    def _preparse(args: str) -> Tuple[List[Argument], List[Argument], List[Argument]]:
        positional: List[Argument] = []
        kwarg_only: List[Argument] = []
        out: List[Argument] = []
        arguments_acc = positional

        # TODO: Use a real parser here; this will get bamboozled
        # by signatures that contain things like std::array<bool, 2> (note the space)
        for arg in args.split(", "):
            if not arg:
                continue
            if arg == "*":
                assert (
                    arguments_acc is positional
                ), "invalid syntax: kwarg-only specifier * can only occur once"
                arguments_acc = kwarg_only
                continue
            parg = Argument.parse(arg)
            # Currently, we rely directly on the invariant that there are NO
            # kwarg-only mutating arguments.  If you want to relax this,
            # we will need a more semantic way of matching that takes
            # into account return arguments.  In that case, you will have
            # to manage out computation a level up, in FunctionSchema.  See Note
            # [is_out_fn]
            if parg.annotation is not None and parg.annotation.is_write:
                if arguments_acc is positional:
                    pass  # do nothing
                elif arguments_acc is kwarg_only:
                    arguments_acc = out
            else:
                assert arguments_acc is not out
            arguments_acc.append(parg)

        return positional, kwarg_only, out

    @staticmethod
    def parse(args: str) -> "Arguments":
        """
        Input: 'int x, int y, int z'
        """

        # We do this in two phases.  First we parse into three
        # main categories: positional, kwarg_only, out.
        # Then, we reparse positional and kwarg_only to separate
        # out the self argument and tensor options arguments.

        positional, kwarg_only, out = Arguments._preparse(args)

        # Split self argument
        self_ix = None
        for i, a in enumerate(positional):
            if a.name == "self":
                self_ix = i
                break
        pre_self_positional: List[Argument]
        self_arg: Optional[SelfArgument]
        post_self_positional: List[Argument]
        if self_ix is not None:
            pre_self_positional = positional[:self_ix]
            self_arg = SelfArgument(positional[self_ix])
            post_self_positional = positional[self_ix + 1 :]
        else:
            pre_self_positional = []
            self_arg = None
            post_self_positional = positional

        # Group tensor options arguments
        pre_tensor_options_kwarg_only: List[Argument] = []
        tensor_options: Optional[TensorOptionsArguments] = None
        post_tensor_options_kwarg_only: List[Argument] = []
        kwarg_only_acc = pre_tensor_options_kwarg_only

        def pred(name: str, ty: Type) -> Callable[[Argument], bool]:
            return lambda a: a.name == name and a.type in [ty, OptionalType(ty)]

        predicates = [  # order matters
            pred("dtype", Type.parse("ScalarType")),
            pred("layout", Type.parse("Layout")),
            pred("device", Type.parse("Device")),
            pred("pin_memory", Type.parse("bool")),
        ]

        i = 0
        while i < len(kwarg_only):
            # If there is enough space...
            if i <= len(kwarg_only) - len(predicates):
                # And the next len(predicates) arguments look like TensorOptions arguments
                if all(
                    p(a)
                    for p, a in zip(predicates, kwarg_only[i : i + len(predicates)])
                ):
                    assert kwarg_only_acc is pre_tensor_options_kwarg_only
                    # Group them together as one argument
                    tensor_options = TensorOptionsArguments(
                        dtype=kwarg_only[i],
                        layout=kwarg_only[i + 1],
                        device=kwarg_only[i + 2],
                        pin_memory=kwarg_only[i + 3],
                    )
                    i += len(predicates)
                    kwarg_only_acc = post_tensor_options_kwarg_only
                    continue
            kwarg_only_acc.append(kwarg_only[i])
            i += 1

        return Arguments(
            pre_self_positional=tuple(pre_self_positional),
            self_arg=self_arg,
            post_self_positional=tuple(post_self_positional),
            pre_tensor_options_kwarg_only=tuple(pre_tensor_options_kwarg_only),
            tensor_options=tensor_options,
            post_tensor_options_kwarg_only=tuple(post_tensor_options_kwarg_only),
            out=tuple(out),
        )

    def __str__(self) -> str:
        all_arguments: List[str] = []
        all_arguments.extend(map(str, self.flat_positional))
        if self.flat_kwarg_only or self.out:
            all_arguments.append("*")
        all_arguments.extend(map(str, self.flat_kwarg_only))
        all_arguments.extend(map(str, self.out))
        return ", ".join(all_arguments)

    def __post_init__(self) -> None:
        # TODO: These invariants are weirdly asymmetric?
        # TODO: Fancier types?
        if self.self_arg is None:
            assert not self.pre_self_positional
        if self.tensor_options is None:
            assert not self.post_tensor_options_kwarg_only

        # We don't allow any of the following to have argument annotations,
        # to keep things simple.
        mutable_pre_self_positionals = [
            a
            for a in self.pre_self_positional
            if a.annotation is not None and a.annotation.is_write
        ]
        assert (
            len(mutable_pre_self_positionals) == 0
        ), "mutable pre_self_positional arguments are not currently supported in the schema"


# Names that validly are __iXXX__ indicating inplace operations.
# Taken from https://www.python.org/dev/peps/pep-0203/#new-methods
# NB: PyTorch hasn't actually implemented all of these
AUGMENTED_ASSIGNMENT_NAMES = [
    "add",
    "sub",
    "mul",
    "div",
    "mod",
    "pow",
    "lshift",
    "rshift",
    "and",
    "xor",
    "or",
]

# A BaseOperatorName is what we think of the operator name, without
# the overload name.  Unusually, we don't represent this as just a
# string; instead, we directly represent a few important semantic
# bits of information we derive from the string: namely whether
# or not it's inplace (add_) and whether or not it's a double-underscore
# method (__add__)
@dataclass(frozen=True)
class BaseOperatorName:
    base: str
    inplace: bool
    dunder_method: bool

    @staticmethod
    def parse(op: str) -> "BaseOperatorName":
        assert op != ""
        assert not op.endswith("_out"), (
            "_out suffix is reserved and not permitted for operator names; "
            "did you mean to specify an out overload name instead?"
        )
        m = re.match(r"^__([^_]+)__$", op)
        if m is not None:
            dunder_method = True
            base = m.group(1)
            if any(base == f"i{n}" for n in AUGMENTED_ASSIGNMENT_NAMES):
                inplace = True
                base = base[1:]
            else:
                inplace = False
                # temporary, this is not intrinsically true but
                # has been historically true for dunder methods
                # we support  (but, if we ever got, say, __int__, this would
                # be wrong!)
                assert base[0] != "i"
        else:
            dunder_method = False
            base = op
            if base[-1] == "_":
                inplace = True
                base = base[:-1]
            else:
                inplace = False
        r = BaseOperatorName(base=base, inplace=inplace, dunder_method=dunder_method)
        assert str(r) == op, f"{str(r)} != {op}"
        return r

    def __str__(self) -> str:
        if self.dunder_method:
            i = "i" if self.inplace else ""
            return f"__{i}{self.base}__"
        else:
            i = "_" if self.inplace else ""
            return f"{self.base}{i}"


# Operator name is the base operator name along with the (typically not
# user visible) overload string.
@dataclass(frozen=True)
class OperatorName:
    name: BaseOperatorName
    overload_name: str

    @staticmethod
    def parse(op_name: str) -> "OperatorName":
        if "." in op_name:
            name, overload_name = op_name.split(".", 1)
        else:
            name = op_name
            overload_name = ""
        r = OperatorName(name=BaseOperatorName.parse(name), overload_name=overload_name)
        assert str(r) == op_name, f"{str(r)} != {op_name}"
        return r

    def __str__(self) -> str:
        if self.overload_name:
            return f"{self.name}.{self.overload_name}"
        else:
            return f"{self.name}"

    # NB: This must be synchronized with the naming scheme in
    # aten/src/ATen/templates/Operators.h
    # Given a function schema "aten::op.overload(...)",
    # If there is no overload name, this returns f"{op}"
    # If there is an overload name, this returns f"{op}_{overload}"
    def unambiguous_name(self) -> str:
        if self.overload_name:
            return f"{self.name}_{self.overload_name}"
        else:
            return f"{self.name}"

    def remove_inplace(self) -> "OperatorName":
        return OperatorName(
            name=BaseOperatorName(
                base=self.name.base,
                inplace=False,
                dunder_method=self.name.dunder_method,
            ),
            overload_name=self.overload_name,
        )

    def with_overload(self, overload: str) -> "OperatorName":
        return OperatorName(
            name=BaseOperatorName(
                base=self.name.base,
                inplace=False,
                dunder_method=self.name.dunder_method,
            ),
            overload_name=overload,
        )


def gets_generated_out_inplace_wrapper(
    f: NativeFunction, g: NativeFunctionsGroup, b: BackendIndex
) -> bool:
    return (
        f.func.kind() is not SchemaKind.functional
        and not b.has_kernel(f)
        and b.has_kernel(g.functional)
    )


# NativeFunction objects that are views (f.is_view_op returns True)
# are added into a `NativeFunctionsViewGroup`, which we can use to
# easily access the generated (optional) view_copy NativeFunction.
# It's convenient to group them together, so we pair them up in NativeFunctionsViewGroup.
# See Note [Codegen'd {view}_copy Operators]
#
# One property of this representation is that in order for a view-like op to be part of
# a NativeFunctionsViewGroup, the "aliasing" version of that view op must exist.
# There's one case where that doesn't happen: we have a non-aliasing `narrow_copy.out` op,
# but don't have corresponding aliasing `narrow.out` op.
# This means that `narrow_copy.out` won't appear as a NativeFunctionsViewGroup.
@dataclass(frozen=True)
class NativeFunctionsViewGroup:
    view: NativeFunction
    # Note: the {view}_copy operator is optional because we currently don't generate copy variants
    # for all view ops. Notably, we don't generate them for CompositeImplicitAutograd views
    # (we already get them "for free" through decomposition)
    view_copy: Optional[NativeFunction]
    # view_inplace ops are also optional, but every view_inplace op should have out-of-place variant.
    view_inplace: Optional[NativeFunction]

    def __post_init__(self) -> None:
        assert self.view.is_view_op
        if self.view_copy is None:
            assert not gets_generated_view_copy(self.view), (
                f"{str(self.view.func.name)} appears to be a new operator that aliases its inputs."
                " The codegen expects you to add a corresponding operator to native_functions.yaml:"
                f" {get_view_copy_name(self.view)!s}."
                " See Note [view_copy NativeFunctions] for details."
            )
        else:
            assert self.view_copy.func.name.name.base.endswith("_copy")
            assert self.view.func.signature() == self.view_copy.func.signature(
                strip_view_copy_name=True
            )
            assert "view_copy" in self.view_copy.tags, (
                f"{str(self.view_copy.func.name), str(self.view.tags)} appears to be a view_copy operator. The codegen expects"
                " view_copy operators to be annotated with the 'view_copy' tag in native_functions.yaml."
                " See Note [view_copy NativeFunction] for details."
            )
        if self.view_inplace is not None:
            assert self.view.func.signature() == self.view_inplace.func.signature()

        if self.view.has_composite_implicit_autograd_kernel:
            if self.view_inplace is not None:
                assert self.view_inplace.has_composite_implicit_autograd_kernel, (
                    f"{str(self.view.func.name)} and {str(self.view_inplace.func.name)} must either"
                    " both have CompositeImplicitAutograd kernels, or both not have composite kernels."
                )

    def functions(self, *, include_copy: bool = True) -> Iterator[NativeFunction]:
        yield self.view
        if self.view_inplace is not None:
            yield self.view_inplace
        if self.view_copy is not None and include_copy:
            yield self.view_copy

    @property
    def root_name(self) -> str:
        return self.view.root_name

    @property
    def composite(self) -> bool:
        # We currently assert that the "group" is consistent.
        # If the view op is composite, then its view_inplace op is too.
        return self.view.has_composite_implicit_autograd_kernel


def gets_generated_view_copy(f: NativeFunction) -> bool:
    # Only aliasing (view) operators get a copy variant.
    if not f.is_view_op:
        return False
    # We don't need to bother generating copy variants for CompositeImplicitAutograd ops,
    # because we can let them decompose into base view ops.
    if f.has_composite_implicit_autograd_kernel:
        return False
    # We also don't need to generate copy variants for inplace views.
    if "inplace_view" in f.tags:
        return False
    return True


# Given a NativeFunction that corresponds to a view op,
# returns the OperatorName of the corresponding "copy" variant of the op.
def get_view_copy_name(f: NativeFunction) -> "OperatorName":
    # Right now, when asking for a view op's corresponding "view_copy" name
    # we assert for sanity that the op is allowed to have a generated view_copy variant.
    # (We can do this because "gets_generated_view_copy()" tell us which ops get a generated view_copy op).
    # However, narrow_copy() already exists as an op directly in native_functions.yaml.
    # I'm hardcoding narrow_copy here for now to maintain the assert,
    # But we could also just get rid of the assert.
    list_of_ops_with_explicit_view_copy_operators = ["narrow"]
    if str(f.func.name) not in list_of_ops_with_explicit_view_copy_operators:
        assert gets_generated_view_copy(f)

    base_name = f"{f.func.name.name.base}_copy"
    view_copy_name = OperatorName(
        name=BaseOperatorName(
            base=base_name, inplace=False, dunder_method=f.func.name.name.dunder_method
        ),
        overload_name=f.func.name.overload_name,
    )
    return view_copy_name


# Helper functions for parsing argument lists (both inputs and returns)


def parse_returns(return_decl: str) -> Tuple[Return, ...]:
    """
    Input: '()'
    Output: []
    """
    if return_decl == "()":
        return ()
    if return_decl[0] == "(" and return_decl[-1] == ")":
        return_decl = return_decl[1:-1]
    return tuple(Return.parse(arg) for arg in return_decl.split(", "))


# A Precompute instance consists of a map from kernel argument name
# to the list of Argument instances that should replace that
# kernel argument in the impl function.
@dataclass(frozen=True)
class Precompute:
    # A map from kernel argument name -> a list of precomputed
    # elements that replaces/supersedes it.
    replace: Dict[str, List[Argument]]
    # List of precomputed args added without replacement
    add: List[Argument]

    @staticmethod
    def parse(src: object) -> "Precompute":
        assert isinstance(src, list)

        # src is a list of strings of the format:
        #   {kernel param name} -> {replacement decl}[, {replacement decl}, ...]
        #   [{add decl}[, {add decl}, ...]]
        # The last line is optional and contains the precomputed parameters that are
        # added without replacement.
        # The other lines are parsed to get the names of which precomputed elements
        # should replace which kernel arguments.
        add_args = []
        if " -> " not in src[-1]:
            add_list = src[-1].split(",")
            add_args = [Argument.parse(name.strip()) for name in add_list]
            src = src[:-1]

        replace = {}
        for raw_replace_item in src:
            assert isinstance(raw_replace_item, str)
            assert " -> " in raw_replace_item, (
                "precomputed parameters without replacement"
                " are allowed only in the last line"
            )

            arg, with_list_raw = raw_replace_item.split(" -> ")
            with_list = with_list_raw.split(",")
            with_list_args = [Argument.parse(name.strip()) for name in with_list]
            replace[arg] = with_list_args

        r = Precompute(replace=replace, add=add_args)
        assert r.to_list() == src, "r.to_list() != src"
        return r

    def to_list(self) -> List[str]:
        replace_list = []
        for kernel_param, replacement_params in self.replace.items():
            replacements = ", ".join(str(param) for param in replacement_params)
            replace_list.append(f"{kernel_param} -> {replacements}")

        return replace_list


import torchgen.api.ufunc as ufunc