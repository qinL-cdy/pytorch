from collections import defaultdict
import copy
import dataclasses
import sympy
from typing import Any, Dict, List, Optional, Tuple, Union
from torch._functorch.aot_autograd import FQN, GraphInputName, GraphOutputName
from torch._export.functionalize_assertions import (
    _functionalize_side_effectful_ops,
    SideEffectOpsFunctionalizationResult,
)


import torch
from torch.fx.passes.pass_manager import PassManager
import torch.fx._pytree as fx_pytree
import torch.utils._pytree as pytree
from torch.fx.experimental.symbolic_shapes import SymInt
from torch._subclasses.fake_tensor import FakeTensor
from . import error
from .pass_base import PassType
from .passes.add_runtime_assertions_for_constraints_pass import (
    _AddRuntimeAssertionsForConstraintsPass,
    InputDim,
    RangeConstraint,
)


__all__ = ["ExportedProgram"]


LeafValue = Union[
    None,
    bool,
    complex,
    float,
    int,
    str,
    torch.Tensor,
    torch.device,
    torch.dtype,
    torch.layout,
    torch.memory_format,
]


# Information to maintain user calling/returning specs
@dataclasses.dataclass
class CallSpec:
    in_spec: pytree.TreeSpec
    out_spec: pytree.TreeSpec


# Extra information for joint graphs
@dataclasses.dataclass
class ExportBackwardSignature:
    gradients_to_parameters: Dict[str, str]
    gradients_to_user_inputs: Dict[str, str]
    loss_output: str


@dataclasses.dataclass
class ExportGraphSignature:
    parameters: List[FQN]
    buffers: List[FQN]

    user_inputs: List[GraphInputName]
    user_outputs: List[GraphOutputName]
    inputs_to_parameters: Dict[GraphInputName, FQN]
    inputs_to_buffers: Dict[GraphInputName, FQN]

    buffers_to_mutate: Dict[GraphOutputName, FQN]

    backward_signature: Optional[ExportBackwardSignature]

    assertion_dep_token_output: Optional[FQN] = None
    assertion_dep_token_index: Optional[int] = None

    def __post_init__(self) -> None:
        assert (
            self.assertion_dep_token_output is not None
            and self.assertion_dep_token_index is not None
        ) or (
            self.assertion_dep_token_output is None
            and self.assertion_dep_token_index is None
        )

        if self.assertion_dep_token_index is not None:
            assert (
                len(self.user_inputs) + len(self.buffers_to_mutate)
                == self.assertion_dep_token_index
            )


class ExportedProgram:
    def __init__(
        self,
        root: Union[torch.nn.Module, Dict[str, Any]],
        graph: torch.fx.Graph,
        graph_signature: ExportGraphSignature,
        call_spec: CallSpec,
        state_dict: Dict[str, Union[torch.Tensor, torch.nn.Parameter]],
        range_constraints: Dict[sympy.Symbol, RangeConstraint],
        equality_constraints: List[Tuple[InputDim, InputDim]],
    ):
        # Remove codegen related things from the graph. It should just be a flat graph.
        graph._codegen = torch.fx.graph.CodeGen()
        self.graph_module = torch.fx.GraphModule(root, graph)

        self.graph_signature: ExportGraphSignature = graph_signature
        self.call_spec: CallSpec = call_spec
        self.state_dict: Dict[str, Any] = state_dict
        self.range_constraints: Dict[sympy.Symbol, RangeConstraint] = range_constraints
        self.equality_constraints: List[Tuple[InputDim, InputDim]] = equality_constraints

    def __call__(self, *args: Any) -> Any:
        if self.call_spec.in_spec is not None:
            try:
                args = fx_pytree.tree_flatten_spec(args, self.call_spec.in_spec)  # type: ignore[assignment]
            except Exception:
                _, received_spec = pytree.tree_flatten(args)
                raise error.InternalError(
                    "Trying to flatten user inputs with exported input tree spec: \n"
                    f"{self.call_spec.in_spec}\n"
                    "but actually got inputs with tree spec of: \n"
                    f"{received_spec}"
                )

        param_buffer_values = (value for _, value in self.state_dict.items())

        with torch.no_grad():
            res = torch.fx.Interpreter(self.graph_module).run(
                *param_buffer_values,
                *args,
                enable_io_processing=False
            )

        if self.call_spec.out_spec is not None:
            mutation = self.graph_signature.buffers_to_mutate
            num_mutated = len(mutation)
            mutated_buffers = res[:num_mutated]

            # Exclude dependency token from final result.
            if self.graph_signature.assertion_dep_token_index is not None:
                res = res[:self.graph_signature.assertion_dep_token_index]

            res = res[num_mutated:]
            try:
                res = pytree.tree_unflatten(res, self.call_spec.out_spec)
            except Exception:
                _, received_spec = pytree.tree_flatten(res)
                raise error.InternalError(
                    "Trying to flatten user outputs with exported output tree spec: \n"
                    f"{self.call_spec.out_spec}\n"
                    "but actually got outputs with tree spec of: \n"
                    f"{received_spec}"
                )
            finally:
                ix = 0
                for _, buffer in self.graph_signature.buffers_to_mutate.items():
                    self.state_dict[buffer] = mutated_buffers[ix]
                    ix += 1
        return res

    def __str__(self) -> str:
        graph_module = self.graph_module.print_readable(print_output=False).replace("\n", "\n    ")
        string = (
            "ExportedProgram:\n"
            f"    {graph_module}\n"
            f"Graph Signature: {self.graph_signature}\n"
            f"Symbol to range: {self.range_constraints}\n"
        )
        return string

    @property
    def graph(self):
        return self.graph_module.graph

    def transform(self, *passes: PassType) -> "ExportedProgram":
        pm = PassManager(list(passes))
        res = pm(self.graph_module)
        transformed_gm = res.graph_module if res is not None else self.graph_module
        assert transformed_gm is not None
        transformed_ep = ExportedProgram(
            transformed_gm,
            transformed_gm.graph,
            copy.deepcopy(self.graph_signature),
            copy.deepcopy(self.call_spec),
            self.state_dict,
            copy.deepcopy(self.range_constraints),
            copy.deepcopy(self.equality_constraints),
        )
        return transformed_ep

    def _add_runtime_assertions(
        self, functionalize_assertions: bool,
    ) -> "ExportedProgram":
        ep = self.transform(
            _AddRuntimeAssertionsForConstraintsPass(
                self.range_constraints,
                self.equality_constraints,
            ),
        )
        if functionalize_assertions:
            # Ideally `_update_after_adding_runtime_assertions` should run
            # whenever `_AddRuntimeAssertionsForConstraintsPass` runs, here
            # bundle it with `functionalize_assertions` for safety.
            ep = _update_after_adding_runtime_assertions(self, ep)
            ep = _update_after_functionalizing_runtime_assertions(
                ep,
                _functionalize_side_effectful_ops(gm=ep.graph_module),
            )

        return ep


def _update_after_functionalizing_runtime_assertions(
    ep: ExportedProgram,
    result: SideEffectOpsFunctionalizationResult,
) -> ExportedProgram:
    graph_signature = dataclasses.replace(
        copy.deepcopy(ep.graph_signature),
        assertion_dep_token_output=result.dep_token_output,
        assertion_dep_token_index=result.dep_token_output_index,
    )
    return _update_exported_program(
        ep,
        graph_module=result.graph_module,
        graph_signature=graph_signature,
    )


def _update_after_adding_runtime_assertions(
    ep: ExportedProgram,
    new_ep: ExportedProgram,
) -> ExportedProgram:
    # TODO: Improve current pass infra to make it possible to update graph
    # signature as well (currently only graph module).

    def _get_output_FQNs(gm: torch.fx.GraphModule) -> List[FQN]:
        output_node = next(n for n in gm.graph.nodes if n.op == "output")
        return [str(arg) for arg in output_node.args[0]]

    # Update output names since after adding run time assertions, the FQNs of
    # outputs could change.
    # The assumption here is that `_AddRuntimeAssertionsForConstraintsPass`:
    # - Won't change graph outputs order semantically so it's possible to create
    #   map from old to new output FQNs based on position.
    # - Will keep input FQNs unchanged so no need to update inputs related
    #   fields (`user_inputs`, `inputs_to_parameters`, `inputs_to_buffers`, ...)
    outputs = _get_output_FQNs(ep.graph_module)
    new_outputs = _get_output_FQNs(new_ep.graph_module)
    assert len(outputs) == len(new_outputs)
    output_map = dict(zip(outputs, new_outputs))
    gs = ep.graph_signature
    # Need to update graph signature fields related to output since after adding
    # runtime assertions, the output FQNs could change.
    new_user_outputs = [output_map[u] for u in gs.user_outputs]
    new_buffers_to_mutate = {output_map[u]: b for u, b in gs.buffers_to_mutate.items()}

    return _update_exported_program(
        ep=new_ep,
        graph_signature=dataclasses.replace(
            copy.deepcopy(new_ep.graph_signature),
            user_outputs=new_user_outputs,
            buffers_to_mutate=new_buffers_to_mutate,
        )
    )


def _update_exported_program(
    ep: ExportedProgram,
    *,
    graph_module: Optional[torch.fx.GraphModule] = None,
    graph_signature: Optional[ExportGraphSignature] = None,
) -> ExportedProgram:
    if graph_module is None and graph_signature is None:
        return ep

    gm = copy.deepcopy(ep.graph_module) if graph_module is None else graph_module
    gs = (
        copy.deepcopy(ep.graph_signature)
        if graph_signature is None
        else graph_signature
    )
    return ExportedProgram(
        root=gm,
        graph=gm.graph,
        graph_signature=gs,
        call_spec=copy.deepcopy(ep.call_spec),
        state_dict=ep.state_dict,
        range_constraints=copy.deepcopy(ep.range_constraints),
        equality_constraints=copy.deepcopy(ep.equality_constraints),
    )


def _process_constraints(
    graph_module: torch.fx.GraphModule,
    graph_signature: ExportGraphSignature,
    example_inputs: List[torch.Tensor],
) -> Tuple[Dict[sympy.Symbol, RangeConstraint], List[Tuple[InputDim, InputDim]]]:
    """
    Process the constraints stored in the graph module to return something more readable.

    Args:
        graph_module (torch.fx.GraphModule): GraphModule returned from
            dynamo.export, which contains the "input_shape_constraints" and
            "inline_constraints" metadata

        example_inputs: Flattened list of example inputs used to export the graph module

    Returns:
        range_constraints (Dict[sympy.Symbol, RangeConstraints]): Mapping of
            symbols (from SymInts) appearing in the fake tensors in
            node.meta["val"] to their range constraints, which are a tuple
            containing (lower, upper) constraints.

        equality_constraints (List[Tuple[InputDim, InputDim]]): List of tuples
            of (node, dim) to mark that these dimensions are equal.
    """
    input_shape_constraints = graph_module.meta.get("input_shape_constraints", [])
    inline_constraints = graph_module.meta.get("inline_constraints", [])
    num_params_buffer = len(graph_signature.buffers) + len(graph_signature.parameters)

    # Create dict mapping tensor_id to node names
    tensor_id_to_nodes: Dict[int, List[str]] = defaultdict(list)
    # Create dict mapping placeholder node names to their nodes
    placeholder_nodes: Dict[str, torch.fx.Node] = {}
    for i, node in enumerate(graph_module.graph.nodes):
        if node.op != "placeholder":
            # All placeholder nodes should be together in the beginning of the
            # graph
            break
        if i >= num_params_buffer:
            example_input = example_inputs[i - num_params_buffer]
            tensor_id_to_nodes[id(example_input)].append(node.name)
            placeholder_nodes[node.name] = node

    # Create list of (node name, dim) tuples to mark that they are equal
    equality_constraints: List[Tuple[InputDim, InputDim]] = []
    # Create dict mapping (node name, dim) a list of range (lower, upper)
    # constraints
    multi_range_constraints: Dict[InputDim, List[RangeConstraint]] = defaultdict(list)
    for constraint in input_shape_constraints:
        for node in tensor_id_to_nodes[constraint["t_id"]]:
            node_dim = InputDim(node, constraint["dim"])

            # Accumulate range constraints
            multi_range_constraints[node_dim].append(
                RangeConstraint(constraint["min"], constraint["max"])
            )

            # Accumulate equality constraints
            if shared := constraint.get("shared", None):
                for other_node in tensor_id_to_nodes[shared["t_id"]]:
                    other_node_dim = InputDim(other_node, shared["dim"])
                    equality_constraints.append((node_dim, other_node_dim))

    # Create dict mapping symbol to a singular range (lower, upper)
    range_constraints: Dict[sympy.Symbol, RangeConstraint] = {}

    # Add inline constraints to range_constraints
    for symbol, value_range in inline_constraints.items():
        range_constraints[symbol] = RangeConstraint(value_range.lower, value_range.upper)

    # Add input range constraints to range_constraintss
    for input_dim, multi_range_constraint in multi_range_constraints.items():  # type: ignore[assignment]
        # Simplify the range constraints into a single range constraint
        # Ex. ranges [2, 10] and [3, 11] would get merged to [3, 10]
        min_vals = [rc.min_val for rc in multi_range_constraint]
        max_vals = [rc.max_val for rc in multi_range_constraint]
        min_val = max(min_vals)
        max_val = min(max_vals)
        assert min_val <= max_val

        # Add input node range constraints
        val = placeholder_nodes[input_dim.input_name].meta["val"]
        assert isinstance(val, FakeTensor)
        symint = val.shape[input_dim.dim]
        assert isinstance(symint, SymInt)
        symbol = symint.node._expr
        range_constraints[symbol] = RangeConstraint(min_val, max_val)

    return range_constraints, equality_constraints
