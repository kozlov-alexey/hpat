from __future__ import print_function, division, absolute_import

from collections import namedtuple, defaultdict
from functools import reduce
import copy
import numpy as np
import numba
from numba import typeinfer, ir, ir_utils, config, types, compiler
from numba.ir_utils import (visit_vars_inner, replace_vars_inner, remove_dead,
                            compile_to_numba_ir, replace_arg_nodes,
                            replace_vars_stmt, find_callname, guard,
                            mk_unique_var, find_topo_order, is_getitem,
                            build_definitions, remove_dels, get_ir_of_code,
                            get_definition, find_callname, get_name_var_table,
                            replace_var_names)
from numba.parfor import wrap_parfor_blocks, unwrap_parfor_blocks, Parfor
from numba.typing import signature
from numba.typing.templates import infer_global, AbstractTemplate
import hpat
from hpat.utils import is_call, is_var_assign, is_assign, debug_prints
from hpat import distributed, distributed_analysis
from hpat.distributed_analysis import Distribution
from hpat.distributed_lower import _h5_typ_table
from hpat.str_ext import string_type
from hpat.str_arr_ext import string_array_type

AggFuncStruct = namedtuple('AggFuncStruct', ['vars', 'var_typs', 'init',
                                            'update', 'combine', 'eval',
                                            'typemap', 'calltypes',
                                            'redvar_offsets'])


class Aggregate(ir.Stmt):
    def __init__(self, df_out, df_in, key_name, out_key_var, df_out_vars,
                                 df_in_vars, key_arr, agg_func, out_typs, loc):
        # name of output dataframe (just for printing purposes)
        self.df_out = df_out
        # name of input dataframe (just for printing purposes)
        self.df_in = df_in
        # key name (for printing)
        self.key_name = key_name
        self.out_key_var = out_key_var

        self.df_out_vars = df_out_vars
        self.df_in_vars = df_in_vars
        self.key_arr = key_arr

        self.agg_func = agg_func
        self.out_typs = out_typs

        self.loc = loc

    def __repr__(self):  # pragma: no cover
        out_cols = ""
        for (c, v) in self.df_out_vars.items():
            out_cols += "'{}':{}, ".format(c, v.name)
        df_out_str = "{}{{{}}}".format(self.df_out, out_cols)
        in_cols = ""
        for (c, v) in self.df_in_vars.items():
            in_cols += "'{}':{}, ".format(c, v.name)
        df_in_str = "{}{{{}}}".format(self.df_in, in_cols)
        return "aggregate: {} = {} [key: {}:{}] ".format(df_out_str, df_in_str,
                                            self.key_name, self.key_arr.name)


def aggregate_typeinfer(aggregate_node, typeinferer):
    for out_name, out_var in aggregate_node.df_out_vars.items():
        typ = aggregate_node.out_typs[out_name]
        # TODO: are there other non-numpy array types?
        if typ == string_type:
            arr_type = string_array_type
        else:
            arr_type = types.Array(typ, 1, 'C')

        typeinferer.lock_type(out_var.name, arr_type, loc=aggregate_node.loc)

    # return key case
    if aggregate_node.out_key_var is not None:
        in_var = aggregate_node.key_arr
        typeinferer.constraints.append(typeinfer.Propagate(
            dst=aggregate_node.out_key_var.name, src=in_var.name,
            loc=aggregate_node.loc))

    return

typeinfer.typeinfer_extensions[Aggregate] = aggregate_typeinfer


def aggregate_usedefs(aggregate_node, use_set=None, def_set=None):
    if use_set is None:
        use_set = set()
    if def_set is None:
        def_set = set()

    # key array and input columns are used
    use_set.add(aggregate_node.key_arr.name)
    use_set.update({v.name for v in aggregate_node.df_in_vars.values()})

    # output columns are defined
    def_set.update({v.name for v in aggregate_node.df_out_vars.values()})

    # return key is defined
    if aggregate_node.out_key_var is not None:
         def_set.add(aggregate_node.out_key_var.name)

    return numba.analysis._use_defs_result(usemap=use_set, defmap=def_set)


numba.analysis.ir_extension_usedefs[Aggregate] = aggregate_usedefs


def remove_dead_aggregate(aggregate_node, lives, arg_aliases, alias_map, func_ir, typemap):
    #
    dead_cols = []

    for col_name, col_var in aggregate_node.df_out_vars.items():
        if col_var.name not in lives:
            dead_cols.append(col_name)

    for cname in dead_cols:
        aggregate_node.df_in_vars.pop(cname)
        aggregate_node.df_out_vars.pop(cname)

    out_key_var = aggregate_node.out_key_var
    if out_key_var is not None and out_key_var.name not in lives:
        aggregate_node.out_key_var = None

    # TODO: test agg remove
    # remove empty aggregate node
    if (len(aggregate_node.df_out_vars) == 0
            and aggregate_node.out_key_var is None):
        return None

    return aggregate_node


ir_utils.remove_dead_extensions[Aggregate] = remove_dead_aggregate

def get_copies_aggregate(aggregate_node, typemap):
    # aggregate doesn't generate copies, it just kills the output columns
    kill_set = set(v.name for v in aggregate_node.df_out_vars.values())
    if aggregate_node.out_key_var is not None:
        kill_set.add(aggregate_node.out_key_var.name)
    return set(), kill_set


ir_utils.copy_propagate_extensions[Aggregate] = get_copies_aggregate


def apply_copies_aggregate(aggregate_node, var_dict, name_var_table,
                        typemap, calltypes, save_copies):
    """apply copy propagate in aggregate node"""
    aggregate_node.key_arr = replace_vars_inner(aggregate_node.key_arr,
                                                                     var_dict)

    for col_name in list(aggregate_node.df_in_vars.keys()):
        aggregate_node.df_in_vars[col_name] = replace_vars_inner(
            aggregate_node.df_in_vars[col_name], var_dict)
    for col_name in list(aggregate_node.df_out_vars.keys()):
        aggregate_node.df_out_vars[col_name] = replace_vars_inner(
            aggregate_node.df_out_vars[col_name], var_dict)

    if aggregate_node.out_key_var is not None:
        aggregate_node.out_key_var = replace_vars_inner(
            aggregate_node.out_key_var, var_dict)

    return


ir_utils.apply_copy_propagate_extensions[Aggregate] = apply_copies_aggregate


def visit_vars_aggregate(aggregate_node, callback, cbdata):
    if debug_prints():  # pragma: no cover
        print("visiting aggregate vars for:", aggregate_node)
        print("cbdata: ", sorted(cbdata.items()))

    aggregate_node.key_arr = visit_vars_inner(
        aggregate_node.key_arr, callback, cbdata)

    for col_name in list(aggregate_node.df_in_vars.keys()):
        aggregate_node.df_in_vars[col_name] = visit_vars_inner(
            aggregate_node.df_in_vars[col_name], callback, cbdata)
    for col_name in list(aggregate_node.df_out_vars.keys()):
        aggregate_node.df_out_vars[col_name] = visit_vars_inner(
            aggregate_node.df_out_vars[col_name], callback, cbdata)

    if aggregate_node.out_key_var is not None:
        aggregate_node.out_key_var = visit_vars_inner(
            aggregate_node.out_key_var, callback, cbdata)


# add call to visit aggregate variable
ir_utils.visit_vars_extensions[Aggregate] = visit_vars_aggregate


def aggregate_array_analysis(aggregate_node, equiv_set, typemap,
                                                            array_analysis):
    # empty aggregate nodes should be deleted in remove dead
    assert len(aggregate_node.df_in_vars) > 0 or aggregate_node.out_key_var is not None, ("empty aggregate in array"
                                                                   "analysis")

    # arrays of input df have same size in first dimension as key array
    # string array doesn't have shape in array analysis
    key_typ = typemap[aggregate_node.key_arr.name]
    if key_typ == string_array_type:
        all_shapes = []
    else:
        col_shape = equiv_set.get_shape(aggregate_node.key_arr)
        all_shapes = [col_shape[0]]

    for _, col_var in aggregate_node.df_in_vars.items():
        typ = typemap[col_var.name]
        if typ == string_array_type:
            continue
        col_shape = equiv_set.get_shape(col_var)
        all_shapes.append(col_shape[0])

    if len(all_shapes) > 1:
        equiv_set.insert_equiv(*all_shapes)

    # create correlations for output arrays
    # arrays of output df have same size in first dimension
    # gen size variable for an output column
    post = []
    all_shapes = []
    out_vars = list(aggregate_node.df_out_vars.values())
    if aggregate_node.out_key_var is not None:
        out_vars.append(aggregate_node.out_key_var)

    for col_var in out_vars:
        typ = typemap[col_var.name]
        if typ == string_array_type:
            continue
        (shape, c_post) = array_analysis._gen_shape_call(
            equiv_set, col_var, typ.ndim, None)
        equiv_set.insert_equiv(col_var, shape)
        post.extend(c_post)
        all_shapes.append(shape[0])
        equiv_set.define(col_var)

    if len(all_shapes) > 1:
        equiv_set.insert_equiv(*all_shapes)

    return [], post


numba.array_analysis.array_analysis_extensions[Aggregate] = aggregate_array_analysis


def aggregate_distributed_analysis(aggregate_node, array_dists):
    # input columns have same distribution
    in_dist = Distribution.OneD
    for _, col_var in aggregate_node.df_in_vars.items():
        in_dist = Distribution(
            min(in_dist.value, array_dists[col_var.name].value))

    # key arr
    in_dist = Distribution(
        min(in_dist.value, array_dists[aggregate_node.key_arr.name].value))
    for _, col_var in aggregate_node.df_in_vars.items():
        array_dists[col_var.name] = in_dist
    array_dists[aggregate_node.key_arr.name] = in_dist

    # output columns have same distribution
    out_dist = Distribution.OneD_Var
    for _, col_var in aggregate_node.df_out_vars.items():
        # output dist might not be assigned yet
        if col_var.name in array_dists:
            out_dist = Distribution(
                min(out_dist.value, array_dists[col_var.name].value))

    if aggregate_node.out_key_var is not None:
        col_var = aggregate_node.out_key_var
        if col_var.name in array_dists:
            out_dist = Distribution(
                min(out_dist.value, array_dists[col_var.name].value))

    # out dist should meet input dist (e.g. REP in causes REP out)
    out_dist = Distribution(min(out_dist.value, in_dist.value))
    for _, col_var in aggregate_node.df_out_vars.items():
        array_dists[col_var.name] = out_dist

    if aggregate_node.out_key_var is not None:
        array_dists[aggregate_node.out_key_var.name] = out_dist

    # output can cause input REP
    if out_dist != Distribution.OneD_Var:
        array_dists[aggregate_node.key_arr.name] = out_dist
        for _, col_var in aggregate_node.df_in_vars.items():
            array_dists[col_var.name] = out_dist

    return


distributed_analysis.distributed_analysis_extensions[Aggregate] = aggregate_distributed_analysis

def __update_redvars():
    pass

@infer_global(__update_redvars)
class UpdateDummyTyper(AbstractTemplate):
    def generic(self, args, kws):
        assert not kws
        return signature(types.void, *args)

def __combine_redvars():
    pass

@infer_global(__combine_redvars)
class CombineDummyTyper(AbstractTemplate):
    def generic(self, args, kws):
        assert not kws
        return signature(types.void, *args)

def __eval_res():
    pass

@infer_global(__eval_res)
class EvalDummyTyper(AbstractTemplate):
    def generic(self, args, kws):
        assert not kws
        # takes the output array as first argument to know the output dtype
        return signature(args[0].dtype, *args)

def agg_distributed_run(agg_node, array_dists, typemap, calltypes, typingctx, targetctx):
    parallel = True
    for v in (list(agg_node.df_in_vars.values())
              + list(agg_node.df_out_vars.values()) + [agg_node.key_arr]):
        if (array_dists[v.name] != distributed.Distribution.OneD
                and array_dists[v.name] != distributed.Distribution.OneD_Var):
            parallel = False
        # TODO: check supported types
        # if (typemap[v.name] != types.Array(types.intp, 1, 'C')
        #         and typemap[v.name] != types.Array(types.float64, 1, 'C')):
        #     raise ValueError(
        #         "Only int64 and float64 columns are currently supported in aggregate")
        # if (typemap[left_key_var.name] != types.Array(types.intp, 1, 'C')
        #     or typemap[right_key_var.name] != types.Array(types.intp, 1, 'C')):
        # raise ValueError("Only int64 keys are currently supported in aggregate")

    # TODO: rebalance if output distributions are 1D instead of 1D_Var

    # TODO: handle key column being part of output

    key_typ = typemap[agg_node.key_arr.name]
    # get column variables
    in_col_vars = [v for (n, v) in sorted(agg_node.df_in_vars.items())]
    out_col_vars = [v for (n, v) in sorted(agg_node.df_out_vars.items())]
    # get column types
    in_col_typs = [typemap[v.name] for v in in_col_vars]
    out_col_typs = [typemap[v.name] for v in out_col_vars]
    arg_typs = tuple([key_typ] + in_col_typs)

    agg_func_struct = get_agg_func_struct(agg_node.agg_func, in_col_typs,
                                                          typingctx, targetctx)

    return_key = agg_node.out_key_var is not None

    if parallel:
        agg_impl = gen_agg_func(agg_func_struct, key_typ, in_col_typs,
           out_col_typs, typingctx, typemap, calltypes, targetctx, return_key,
           False, True)
        agg_impl_p = gen_agg_func(agg_func_struct, key_typ, in_col_typs,
                  out_col_typs, typingctx, typemap, calltypes, targetctx,
                  return_key, True)
    else:
        agg_impl = gen_agg_func(agg_func_struct, key_typ, in_col_typs,
            out_col_typs, typingctx, typemap, calltypes, targetctx, return_key)
        agg_impl_p = None

    top_level_func = gen_top_level_agg_func(
        key_typ, return_key, agg_func_struct.var_typs, agg_node.out_typs,
        agg_node.df_in_vars.keys(), agg_node.df_out_vars.keys(), parallel)

    f_block = compile_to_numba_ir(top_level_func,
                                  {'hpat': hpat, 'np': np,
                                  'agg_send_recv_counts': agg_send_recv_counts,
                                  'agg_send_recv_counts_str': agg_send_recv_counts_str,
                                  '__agg_func': agg_impl,
                                  '__agg_func_p': agg_impl_p,
                                  'c_alltoallv': hpat.hiframes_api.c_alltoallv,
                                  'convert_len_arr_to_offset': hpat.hiframes_api.convert_len_arr_to_offset,
                                  'int32_typ_enum': np.int32(_h5_typ_table[types.int32]),
                                  'char_typ_enum': np.int32(_h5_typ_table[types.uint8]),
                                  },
                                  typingctx, arg_typs,
                                  typemap, calltypes).blocks.popitem()[1]

    replace_arg_nodes(f_block, [agg_node.key_arr] + in_col_vars)

    tuple_assign = f_block.body[-3]
    assert (is_assign(tuple_assign) and isinstance(tuple_assign.value, ir.Expr)
        and tuple_assign.value.op == 'build_tuple')
    nodes = f_block.body[:-3]

    for i, var in enumerate(agg_node.df_out_vars.values()):
        out_var = tuple_assign.value.items[i]
        nodes.append(ir.Assign(out_var, var, var.loc))

    if return_key:
        nodes.append(ir.Assign(tuple_assign.value.items[len(out_col_vars)], agg_node.out_key_var, agg_node.out_key_var.loc))

    return nodes


distributed.distributed_run_extensions[Aggregate] = agg_distributed_run


def gen_top_level_agg_func(key_typ, return_key, red_var_typs, out_typs,
                                        in_col_names, out_col_names, parallel):
    """create the top level aggregation function by generating text
    """
    num_red_vars = len(red_var_typs)

    # arg names
    in_names = ["in_c{}".format(i) for i in range(len(in_col_names))]
    out_names = ["out_c{}".format(i) for i in range(len(out_col_names))]

    in_args = ", ".join(in_names)
    if in_args != '':
        in_args = ", " + in_args

    func_text = "def f(key_arr{}):\n".format(in_args)

    if parallel:
        # get send/recv counts
        if key_typ == string_array_type:
            func_text += ("    send_counts, recv_counts, send_counts_char, "
                      "recv_counts_char = agg_send_recv_counts_str(key_arr)\n")
            func_text += "    send_disp = hpat.hiframes_join.calc_disp(send_counts)\n"
            func_text += "    recv_disp = hpat.hiframes_join.calc_disp(recv_counts)\n"
            func_text += "    send_disp_char = hpat.hiframes_join.calc_disp(send_counts_char)\n"
            func_text += "    recv_disp_char = hpat.hiframes_join.calc_disp(recv_counts_char)\n"
            func_text += "    n_uniq_keys = send_counts.sum()\n"
            func_text += "    n_uniq_keys_char = send_counts_char.sum()\n"
            func_text += "    recv_size = recv_counts.sum()\n"
            func_text += "    recv_size_chars = recv_counts_char.sum()\n"
        else:
            func_text += "    send_counts, recv_counts = agg_send_recv_counts(key_arr)\n"
            func_text += "    send_disp = hpat.hiframes_join.calc_disp(send_counts)\n"
            func_text += "    recv_disp = hpat.hiframes_join.calc_disp(recv_counts)\n"
            func_text += "    n_uniq_keys = send_counts.sum()\n"
            func_text += "    n_uniq_keys_char = 0\n"
            func_text += "    recv_size = recv_counts.sum()\n"
        # func_text += "    hpat.cprint(n_uniq_keys, recv_size)\n"

        # prepare for shuffle
        # allocate send/recv buffers
        if key_typ == string_array_type:
            func_text += "    recv_key_arr = hpat.str_arr_ext.pre_alloc_string_array(recv_size, recv_size_chars)\n"
        else:
            func_text += "    recv_key_arr = np.empty(recv_size, np.{})\n".format(
                key_typ.dtype)
        for i in range(num_red_vars):
            func_text += "    recv_{} = np.empty(recv_size, np.{})\n".format(
                i, red_var_typs[i])

        # call local aggregate
        send_col_args = ", ".join(["send_{}".format(a) for a in range(num_red_vars)])
        extra_args = ""
        if key_typ == string_array_type:
            send_col_args = "send_key_arr_chars, " + send_col_args
            extra_args = ", send_disp_char"
        func_text += ("    send_key_arr, {} = __agg_func_p(n_uniq_keys, "
                                    "n_uniq_keys_char, key_arr{}, send_disp{})\n").format(
            send_col_args, in_args, extra_args)
        # func_text += "    hpat.cprint(send_key_arr[0], send_in_c0[0])\n"

        # shuffle key arr
        if key_typ == string_array_type:
            func_text += "    offset_ptr = hpat.str_arr_ext.get_offset_ptr(recv_key_arr)\n"
            func_text += "    data_ptr = hpat.str_arr_ext.get_data_ptr(recv_key_arr)\n"
            func_text += ("    c_alltoallv(send_key_arr.ctypes, "
                "offset_ptr, send_counts.ctypes, recv_counts.ctypes, "
                "send_disp.ctypes, recv_disp.ctypes, int32_typ_enum)\n")
            func_text += ("    c_alltoallv(send_key_arr_chars.ctypes, "
                "data_ptr, send_counts_char.ctypes, recv_counts_char.ctypes, "
                "send_disp_char.ctypes, recv_disp_char.ctypes, char_typ_enum)\n")
            func_text += "    convert_len_arr_to_offset(offset_ptr, recv_size)\n"
        else:
            func_text += ("    c_alltoallv(send_key_arr.ctypes, "
                "recv_key_arr.ctypes, send_counts.ctypes, recv_counts.ctypes, "
                "send_disp.ctypes, recv_disp.ctypes, np.int32({}))\n").format(
                _h5_typ_table[key_typ.dtype])

        # shuffle other columns
        for i, a in enumerate(red_var_typs):
            func_text += ("    c_alltoallv(send_{}.ctypes, recv_{}.ctypes, "
            "send_counts.ctypes, recv_counts.ctypes, send_disp.ctypes, "
            "recv_disp.ctypes, np.int32({}))\n").format(i, i, _h5_typ_table[a])

        func_text += "    key_arr = recv_key_arr\n"
        in_names = ["recv_{}".format(i) for i in range(num_red_vars)]
        # func_text += "    print(hpat.distributed_api.get_rank(), key_arr)\n"

    func_text += "    n_uniq_keys = len(set(key_arr))\n"
    # allocate output
    for i, col in enumerate(in_col_names):
        func_text += "    out_c{} = np.empty(n_uniq_keys, np.{})\n".format(
                                                i, out_typs[col])

    out_key = ""
    if return_key:
        out_key = "out_key = "

    in_args = ", ".join(out_names + in_names)
    if len(out_names) != 0:
        in_args = ", " + in_args

    func_text += "    {}__agg_func(n_uniq_keys, 0, key_arr{})\n".format(
        out_key, in_args)

    out_tup = ", ".join(out_names + ['out_key'] if return_key else out_names)
    func_text += "    return ({},)\n".format(out_tup)

    # print(func_text)

    loc_vars = {}
    exec(func_text, {}, loc_vars)
    f = loc_vars['f']
    return f


def gen_agg_func(agg_func_struct, key_typ, in_typs, out_typs, typingctx,
                 typemap, calltypes, targetctx, return_key,
                 parallel_local=False, parallel_combine=False):
    # has 3 modes: 1- aggregate input column to output (sequential case)
    #              2- aggregate input column to reduce arrays for communication (parallel_local)
    #              3- aggregate received reduce arrays to output (parallel_combine)

    extra_arg_typs = []
    # in combine phase after shuffle, inputs are reduce vars
    if parallel_combine:
        in_typs = [types.Array(t, 1, 'C') for t in agg_func_struct.var_typs]

    # no output columns in parallel-local computation (reduce arrs returned)
    if parallel_local:
        assert not parallel_combine
        out_typs = []
        # add send_disp arg
        extra_arg_typs = [types.Array(types.int32, 1, 'C')]
        if key_typ == string_array_type:
            # add send_disp_char arg
            extra_arg_typs.append(types.Array(types.int32, 1, 'C'))

    arg_typs = tuple([types.intp, types.intp, key_typ] + out_typs + in_typs + extra_arg_typs)

    num_red_vars = len(agg_func_struct.vars)

    iter_func = gen_agg_iter_func(
        key_typ, agg_func_struct.var_typs, len(in_typs), len(out_typs),
        num_red_vars, agg_func_struct.redvar_offsets, return_key,
        parallel_local, parallel_combine)

    _globals = {'hpat': hpat, 'np': np, 'str_copy': hpat.hiframes_api.str_copy,
            'setitem_string_array': hpat.str_arr_ext.setitem_string_array,
            'get_offset_ptr': hpat.str_arr_ext.get_offset_ptr,
            'get_data_ptr': hpat.str_arr_ext.get_data_ptr}
    for i in range(len(agg_func_struct.update)):
        _globals['__update_redvars_{}'.format(i)] = agg_func_struct.update[i]
        _globals['__combine_redvars_{}'.format(i)] = agg_func_struct.combine[i]
        _globals['__eval_res_{}'.format(i)] = agg_func_struct.eval[i]

    f_ir = compile_to_numba_ir(iter_func,
                                  _globals,
                                  typingctx, arg_typs,
                                  typemap, calltypes)

    f_ir._definitions = build_definitions(f_ir.blocks)
    topo_order = find_topo_order(f_ir.blocks)
    first_block = f_ir.blocks[topo_order[0]]

    # deep copy the nodes since they can be reused
    init_nodes = copy.deepcopy(agg_func_struct.init)

    # find reduce variables from names and store in the same order
    reduce_vars = [0] * num_red_vars
    for node in init_nodes:
        if isinstance(node, ir.Assign) and node.target.name in agg_func_struct.vars:
            var_ind = agg_func_struct.vars.index(node.target.name)
            reduce_vars[var_ind] = node.target
    assert 0 not in reduce_vars

    # add initialization code to first block
    # make sure arg nodes are in the beginning
    arg_nodes = []
    for i in range(len(arg_typs)):
        arg_nodes.append(first_block.body[i])
    first_block.body = arg_nodes + init_nodes + first_block.body[len(arg_nodes):]

    # replace init and eval sentinels
    # TODO: replace with functions
    for l in topo_order:
        block = f_ir.blocks[l]
        for i, stmt in enumerate(block.body):
            if isinstance(stmt, ir.Assign) and stmt.target.name.startswith("_init_val_"):
                first_dot = stmt.target.name.index(".")
                var_ind = int(stmt.target.name[len("_init_val_"):first_dot])
                stmt.value = reduce_vars[var_ind]

    return_typ = typemap[f_ir.blocks[topo_order[-1]].body[-1].value.name]

    # compile implementation to binary (Dispatcher)
    agg_impl_func = compiler.compile_ir(
            typingctx,
            targetctx,
            f_ir,
            arg_typs,
            return_typ,
            compiler.DEFAULT_FLAGS,
            {}
    )

    imp_dis = numba.targets.registry.dispatcher_registry['cpu'](iter_func)
    imp_dis.add_overload(agg_impl_func)
    return imp_dis

def gen_agg_iter_func(key_typ, red_var_typs, num_ins, num_outs, num_red_vars,
                 redvar_offsets, return_key, parallel_local, parallel_combine):
    # arg names
    in_names = ["in_c{}".format(i) for i in range(num_ins)]
    out_names = ["out_c{}".format(i) for i in range(num_outs)]

    # number of actual column intpus (parallel_combine case)
    num_col_ins = len(redvar_offsets) - 1

    redvar_arrnames = ", ".join(["redvar_{}_arr".format(i)
                                    for i in range(num_red_vars)])

    extra_args = ""
    if parallel_local:
        # needed due to alltoallv
        extra_args = ", send_disp"
        if key_typ == string_array_type:
            extra_args += ", send_disp_char"

    in_args = ", ".join(out_names + in_names)
    if num_ins != 0:
        in_args = ", " + in_args

    func_text = "def f(n_uniq_keys, n_uniq_keys_char, key_arr{}{}):\n".format(
        in_args, extra_args)

    # allocate reduction var arrays
    for i, typ in enumerate(red_var_typs):
        func_text += "    _init_val_{} = np.{}(0)\n".format(i, typ)
        func_text += "    redvar_{}_arr = np.full(n_uniq_keys, _init_val_{}, np.{})\n".format(
            i, i, typ)

    # key is returned in parallel local agg phase (TODO: avoid if key is output already)
    if parallel_local:
        if key_typ == string_array_type:
            func_text += "    out_key_lens = np.empty(n_uniq_keys, np.uint32)\n"
            func_text += "    out_key_chars = np.empty(n_uniq_keys_char, np.uint8)\n"
        else:
            func_text += "    out_key = np.empty(n_uniq_keys, np.{})\n".format(
                                                                key_typ.dtype)
        func_text += "    n_pes = hpat.distributed_api.get_size()\n"
        func_text += "    tmp_offset = np.zeros(n_pes, dtype=np.int64)\n"
        if key_typ == string_array_type:
            func_text += "    tmp_offset_char = np.zeros(n_pes, dtype=np.int64)\n"
    elif return_key:
        if key_typ == string_array_type:
            func_text += "    out_key =  hpat.str_arr_ext.pre_alloc_string_array(n_uniq_keys, n_uniq_keys_char)\n"
        else:
            func_text += "    out_key = np.empty(n_uniq_keys, np.{})\n".format(
                                                                key_typ.dtype)

    # find write location
    # TODO: non-int dict
    func_text += "    key_write_map = hpat.dict_ext.init_dict_{}_int64()\n".format(
                                                                 key_typ.dtype)

    func_text += "    curr_write_ind = 0\n"
    func_text += "    for i in range(len(key_arr)):\n"
    func_text += "      k = key_arr[i]\n"
    func_text += "      if k not in key_write_map:\n"

    if parallel_local:
        # write to proper buffer location for alltoallv
        func_text += "        node_id = hash(k) % n_pes\n"
        func_text += "        w_ind = send_disp[node_id] + tmp_offset[node_id]\n"
        func_text += "        tmp_offset[node_id] += 1\n"
    else:
        func_text += "        w_ind = curr_write_ind\n"
        func_text += "        curr_write_ind += 1\n"
    func_text += "        key_write_map[k] = w_ind\n"

    if parallel_local:
        if key_typ == string_array_type:
            func_text += "        k_len = len(k)\n"
            func_text += "        out_key_lens[w_ind] = k_len\n"
            func_text += "        w_ind_c = send_disp_char[node_id] + tmp_offset_char[node_id]\n"
            func_text += "        tmp_offset_char[node_id] += k_len\n"
            func_text += "        str_copy(out_key_chars, w_ind_c, k.c_str(), k_len)\n"
        else:
            func_text += "        out_key[w_ind] = k\n"
    elif return_key:
        if key_typ == string_array_type:
            func_text += "        setitem_string_array(get_offset_ptr(out_key), get_data_ptr(out_key), k, w_ind)\n"
        else:
            func_text += "        out_key[w_ind] = k\n"

    func_text += "      else:\n"
    func_text += "        w_ind = key_write_map[k]\n"

    redvar_access = []
    for i in range(num_col_ins):
        redvar_access.append(", ".join(["redvar_{}_arr[w_ind]".format(i)
                            for i in range(redvar_offsets[i], redvar_offsets[i+1])]))

    inarr_access = []
    if parallel_combine:
        for i in range(num_col_ins):
            inarr_access.append(", ".join(["{}[i]".format(a) for a in in_names[redvar_offsets[i]:redvar_offsets[i+1]]]))
        f_name = '__combine_redvars'
    else:
        for i in range(num_col_ins):
            inarr_access.append("{}[i]".format(in_names[i]))
        f_name = '__update_redvars'

    for i in range(num_col_ins):
        func_text += "      {} = {}_{}({}, {})\n".format(
            redvar_access[i], f_name, i, redvar_access[i], inarr_access[i])

    if parallel_local:
        # return out key array and reduce arrays for communication
        if key_typ == string_array_type:
            func_text += "    return out_key_lens, out_key_chars, {}\n".format(redvar_arrnames)
        else:
            func_text += "    return out_key, {}\n".format(redvar_arrnames)
    else:
        # get final output from reduce varis
        if num_col_ins != 0:
            func_text += "    for j in range(n_uniq_keys):\n"
        for i in range(num_col_ins):
            redvar_access = ", ".join(["redvar_{}_arr[j]".format(i)
                                    for i in range(redvar_offsets[i], redvar_offsets[i+1])])
            func_text += "      out_c{}[j] = __eval_res_{}({})\n".format(
                                                       i, i, redvar_access)
        if return_key:
            func_text += "    return out_key\n"

    # print(func_text)

    loc_vars = {}
    exec(func_text, {}, loc_vars)
    f = loc_vars['f']

    return f

@numba.njit
def agg_send_recv_counts(key_arr):
    n_pes = hpat.distributed_api.get_size()
    send_counts = np.zeros(n_pes, np.int32)
    recv_counts = np.empty(n_pes, np.int32)
    key_set = set()
    for i in range(len(key_arr)):
        k = key_arr[i]
        if k not in key_set:
            key_set.add(k)
            node_id = hash(k) % n_pes
            send_counts[node_id] += 1

    hpat.distributed_api.alltoall(send_counts, recv_counts, 1)
    return send_counts, recv_counts

@numba.njit
def agg_send_recv_counts_str(key_arr):
    n_pes = hpat.distributed_api.get_size()
    send_counts = np.zeros(n_pes, np.int32)
    recv_counts = np.empty(n_pes, np.int32)
    send_counts_char = np.zeros(n_pes, np.int32)
    recv_counts_char = np.empty(n_pes, np.int32)
    key_set = hpat.set_ext.init_set_string()
    for i in range(len(key_arr)):
        k = key_arr[i]
        if k not in key_set:
            key_set.add(k)
            node_id = hash(k) % n_pes
            send_counts[node_id] += 1
            send_counts_char[node_id] += len(k)
            hpat.str_ext.del_str(k)

    hpat.distributed_api.alltoall(send_counts, recv_counts, 1)
    hpat.distributed_api.alltoall(send_counts_char, recv_counts_char, 1)
    return send_counts, recv_counts, send_counts_char, recv_counts_char


def compile_to_optimized_ir(func, arg_typs, typingctx):
    # XXX are outside function's globals needed?
    code = func.code if hasattr(func, 'code') else func.__code__
    f_ir = get_ir_of_code({'numba': numba, 'np': np, 'hpat': hpat}, code)

    # rename all variables to avoid conflict (init and eval nodes)
    var_table = get_name_var_table(f_ir.blocks)
    new_var_dict = {}
    for name, var in var_table.items():
        new_var_dict[name] = mk_unique_var(name)
    replace_var_names(f_ir.blocks, new_var_dict)
    f_ir._definitions = build_definitions(f_ir.blocks)

    assert f_ir.arg_count == 1, "agg function should have one input"
    input_name = f_ir.arg_names[0]
    df_pass = hpat.hiframes.HiFrames(f_ir, typingctx,
                           arg_typs, {input_name+":input": "series"})
    df_pass.run()
    remove_dead(f_ir.blocks, f_ir.arg_names, f_ir)
    typemap, return_type, calltypes = compiler.type_inference_stage(
                typingctx, f_ir, arg_typs, None)

    options = numba.targets.cpu.ParallelOptions(True)
    flags = compiler.Flags()
    targetctx = numba.targets.cpu.CPUContext(typingctx)

    DummyPipeline = namedtuple('DummyPipeline',
        ['typingctx', 'targetctx', 'args', 'func_ir', 'typemap', 'return_type',
        'calltypes'])
    pm = DummyPipeline(typingctx, targetctx, None, f_ir, typemap, return_type,
                        calltypes)
    preparfor_pass = numba.parfor.PreParforPass(
            f_ir,
            typemap,
            calltypes, typingctx,
            options
            )
    preparfor_pass.run()
    df_t_pass = hpat.hiframes_typed.HiFramesTyped(f_ir, typingctx, typemap, calltypes)
    df_t_pass.run()
    numba.rewrites.rewrite_registry.apply('after-inference', pm, f_ir)
    parfor_pass = numba.parfor.ParforPass(f_ir, typemap,
    calltypes, return_type, typingctx,
    options, flags)
    parfor_pass.run()
    remove_dels(f_ir.blocks)
    # make sure eval nodes are after the parfor for easier extraction
    # TODO: extract an eval func more robustly
    numba.parfor.maximize_fusion(f_ir, f_ir.blocks, False)
    return f_ir, pm


def get_agg_func_struct(agg_func, in_col_types, typingctx, targetctx):
    """find initialization, update, combine and final evaluation code of the
    aggregation function. Currently assuming that the function is single block
    and has one parfor.
    """
    all_redvars = []
    all_vartypes = []
    all_init_nodes = []
    all_eval_funcs = []
    all_update_funcs = []
    all_combine_funcs = []
    typemap = {}
    calltypes = {}
    # offsets of reduce vars
    curr_offset = 0
    redvar_offsets = [0]

    for in_col_typ in in_col_types:
        f_ir, pm = compile_to_optimized_ir(
            agg_func, tuple([in_col_typ]), typingctx)

        f_ir._definitions = build_definitions(f_ir.blocks)
        # TODO: support multiple top-level blocks
        assert len(f_ir.blocks) == 1 and 0 in f_ir.blocks, ("only simple functions"
                                    " with one block supported for aggregation")
        block = f_ir.blocks[0]

        # find and ignore arg and size/shape nodes for input arr
        block_body = []
        arr_var = None
        for i, stmt in enumerate(block.body):
            if is_assign(stmt) and isinstance(stmt.value, ir.Arg):
                arr_var = stmt.target
                # XXX assuming shape/size nodes are right after arg
                shape_nd = block.body[i+1]
                assert (is_assign(shape_nd) and isinstance(shape_nd.value, ir.Expr)
                    and shape_nd.value.op == 'getattr' and shape_nd.value.attr == 'shape'
                    and shape_nd.value.value.name == arr_var.name)
                shape_vr = shape_nd.target
                size_nd = block.body[i+2]
                assert (is_assign(size_nd) and isinstance(size_nd.value, ir.Expr)
                    and size_nd.value.op == 'static_getitem'
                    and size_nd.value.value.name == shape_vr.name)
                # ignore size/shape vars
                block_body += block.body[i+3:]
                break
            block_body.append(stmt)

        parfor_ind = -1
        for i, stmt in enumerate(block_body):
            if isinstance(stmt, numba.parfor.Parfor):
                assert parfor_ind == -1, "only one parfor for aggregation function"
                parfor_ind = i

        parfor = block_body[parfor_ind]
        remove_dels(parfor.loop_body)
        remove_dels({0: parfor.init_block})

        init_nodes = block_body[:parfor_ind] + parfor.init_block.body
        eval_nodes = block_body[parfor_ind+1:]

        redvars, var_to_redvar = get_parfor_reductions(parfor, parfor.params,
                                                                    pm.calltypes)

        # find reduce variables given their names
        reduce_vars = [0] * len(redvars)
        for stmt in init_nodes:
            if is_assign(stmt) and stmt.target.name in redvars:
                ind = redvars.index(stmt.target.name)
                reduce_vars[ind] = stmt.target
        var_types = [pm.typemap[v] for v in redvars]

        combine_func = gen_combine_func(f_ir, parfor, redvars, var_to_redvar,
            var_types, arr_var, in_col_typ, pm, typingctx, targetctx)

        # XXX: update mutates parfor body
        update_func = gen_update_func(parfor, redvars, var_to_redvar, var_types,
            arr_var, in_col_typ, pm, typingctx, targetctx)

        eval_func = gen_eval_func(f_ir, eval_nodes, reduce_vars, var_types, pm, typingctx, targetctx)

        all_redvars += redvars
        all_vartypes += var_types
        all_init_nodes += init_nodes
        all_eval_funcs.append(eval_func)
        typemap.update(pm.typemap)
        calltypes.update(pm.calltypes)
        all_update_funcs.append(update_func)
        all_combine_funcs.append(combine_func)
        curr_offset += len(redvars)
        redvar_offsets.append(curr_offset)

    return AggFuncStruct(all_redvars, all_vartypes, all_init_nodes,
                         all_update_funcs, all_combine_funcs, all_eval_funcs,
                         typemap, calltypes, redvar_offsets)


def gen_eval_func(f_ir, eval_nodes, reduce_vars, var_types, pm, typingctx, targetctx):

    # eval func takes reduce vars and produces final result
    num_red_vars = len(var_types)
    in_names = ["in{}".format(i) for i in range(num_red_vars)]
    return_typ = pm.typemap[eval_nodes[-1].value.name]

    # TODO: non-numeric return
    func_text = "def f({}):\n return np.{}(0)\n".format(", ".join(in_names), return_typ)

    # print(func_text)
    loc_vars = {}
    exec(func_text, {}, loc_vars)
    f = loc_vars['f']

    arg_typs = tuple(var_types)
    f_ir = compile_to_numba_ir(f, {'numba': numba, 'hpat':hpat, 'np': np},  # TODO: add outside globals
                                  typingctx, arg_typs,
                                  pm.typemap, pm.calltypes)

    # TODO: support multi block eval funcs
    block = list(f_ir.blocks.values())[0]

    # assign inputs to reduce vars used in computation
    assign_nodes = []
    for i, v in enumerate(reduce_vars):
        assign_nodes.append(ir.Assign(block.body[i].target, v, v.loc))
    block.body = block.body[:num_red_vars] + assign_nodes + eval_nodes

    # compile implementation to binary (Dispatcher)
    combine_func = compiler.compile_ir(
            typingctx,
            targetctx,
            f_ir,
            arg_typs,
            return_typ,
            compiler.DEFAULT_FLAGS,
            {}
    )

    imp_dis = numba.targets.registry.dispatcher_registry['cpu'](f)
    imp_dis.add_overload(combine_func)
    return imp_dis


def gen_combine_func(f_ir, parfor, redvars, var_to_redvar, var_types, arr_var,
                       in_col_typ, pm, typingctx, targetctx):
    num_red_vars = len(redvars)
    redvar_in_names = ["v{}".format(i) for i in range(num_red_vars)]
    in_names = ["in{}".format(i) for i in range(num_red_vars)]

    func_text = "def f({}):\n".format(", ".join(redvar_in_names + in_names))

    for bl in parfor.loop_body.values():
        for stmt in bl.body:
            # reduction variables
            if is_assign(stmt) and stmt.target.name in redvars:
                red_var = stmt.target.name
                ind = redvars.index(red_var)
                if len(f_ir._definitions[red_var]) == 2:
                    # 0 is the actual func since init_block is traversed later
                    # in parfor.py:3039, TODO: make this detection more robust
                    var_def = f_ir._definitions[red_var][0]
                    while isinstance(var_def, ir.Var):
                        var_def = guard(get_definition, f_ir, var_def)
                    if (isinstance(var_def, ir.Expr)
                            and var_def.op == 'inplace_binop'
                            and var_def.fn == '+='):
                        func_text += "    v{} += in{}\n".format(ind, ind)
                    if (isinstance(var_def, ir.Expr) and var_def.op == 'call'):
                        fdef = guard(find_callname, f_ir, var_def)
                        if fdef == ('min', 'builtins'):
                            func_text += "    v{} = min(v{}, in{})\n".format(ind, ind, ind)
                        if fdef == ('max', 'builtins'):
                            func_text += "    v{} = max(v{}, in{})\n".format(ind, ind, ind)

    func_text += "    return {}".format(", ".join(["v{}".format(i)
                                                for i in range(num_red_vars)]))
    # print(func_text)
    loc_vars = {}
    exec(func_text, {}, loc_vars)
    f = loc_vars['f']

    # reduction variable types for new input and existing values
    arg_typs = tuple(2 * var_types)

    f_ir = compile_to_numba_ir(f, {'numba': numba, 'hpat':hpat, 'np': np},  # TODO: add outside globals
                                  typingctx, arg_typs,
                                  pm.typemap, pm.calltypes)

    block = list(f_ir.blocks.values())[0]

    return_typ = pm.typemap[block.body[-1].value.name]
    # compile implementation to binary (Dispatcher)
    combine_func = compiler.compile_ir(
            typingctx,
            targetctx,
            f_ir,
            arg_typs,
            return_typ,
            compiler.DEFAULT_FLAGS,
            {}
    )

    imp_dis = numba.targets.registry.dispatcher_registry['cpu'](f)
    imp_dis.add_overload(combine_func)
    return imp_dis

def gen_update_func(parfor, redvars, var_to_redvar, var_types, arr_var,
                       in_col_typ, pm, typingctx, targetctx):
    num_red_vars = len(redvars)
    var_types = [pm.typemap[v] for v in redvars]

    num_in_vars = 1

    # create input value variable for each reduction variable
    in_vars = []
    for i in range(num_in_vars):
        in_var = ir.Var(arr_var.scope, "$input{}".format(i), arr_var.loc)
        in_vars.append(in_var)

    # replace X[i] with input value
    red_ir_vars = [0]*num_red_vars
    for bl in parfor.loop_body.values():
        for stmt in bl.body:
            if is_getitem(stmt) and stmt.value.value.name == arr_var.name:
                stmt.value = in_vars[0]
            # store reduction variables
            if is_assign(stmt) and stmt.target.name in redvars:
                ind = redvars.index(stmt.target.name)
                red_ir_vars[ind] = stmt.target

    redvar_in_names = ["v{}".format(i) for i in range(num_red_vars)]
    in_names = ["in{}".format(i) for i in range(num_in_vars)]

    func_text = "def f({}):\n".format(", ".join(redvar_in_names + in_names))
    func_text += "    __update_redvars()\n"
    func_text += "    return {}".format(", ".join(["v{}".format(i)
                                                for i in range(num_red_vars)]))

    loc_vars = {}
    exec(func_text, {}, loc_vars)
    f = loc_vars['f']

    # XXX input column type can be different than reduction variable type
    arg_typs = tuple(var_types + [in_col_typ.dtype]*num_in_vars)

    f_ir = compile_to_numba_ir(f, {'__update_redvars': __update_redvars},  # TODO: add outside globals
                                  typingctx, arg_typs,
                                  pm.typemap, pm.calltypes)

    f_ir._definitions = build_definitions(f_ir.blocks)

    body = f_ir.blocks.popitem()[1].body
    return_typ = pm.typemap[body[-1].value.name]

    blocks = wrap_parfor_blocks(parfor)
    topo_order = find_topo_order(blocks)
    topo_order = topo_order[1:]  # ignore init block
    unwrap_parfor_blocks(parfor)

    f_ir.blocks = parfor.loop_body
    first_block = f_ir.blocks[topo_order[0]]
    last_block = f_ir.blocks[topo_order[-1]]

    # arg assigns
    initial_assigns = body[:(num_red_vars + num_in_vars)]
    if num_red_vars > 1:
        # return nodes: build_tuple, cast, return
        return_nodes = body[-3:]
        assert (is_assign(return_nodes[0])
            and isinstance(return_nodes[0].value, ir.Expr)
            and return_nodes[0].value.op == 'build_tuple')
    else:
        # return nodes: cast, return
        return_nodes = body[-2:]

    # assign input reduce vars
    # redvar_i = v_i
    for i in range(num_red_vars):
        arg_var = body[i].target
        node = ir.Assign(arg_var, red_ir_vars[i], arg_var.loc)
        initial_assigns.append(node)

    # assign input value vars
    # redvar_in_i = in_i
    for i in range(num_red_vars, num_red_vars + num_in_vars):
        arg_var = body[i].target
        node = ir.Assign(arg_var, in_vars[i-num_red_vars], arg_var.loc)
        initial_assigns.append(node)

    first_block.body = initial_assigns + first_block.body

    # assign ouput reduce vars
    # v_i = red_var_i
    after_assigns = []
    for i in range(num_red_vars):
        arg_var = body[i].target
        node = ir.Assign(red_ir_vars[i], arg_var, arg_var.loc)
        after_assigns.append(node)

    last_block.body += after_assigns + return_nodes

    # TODO: simplify f_ir
    # compile implementation to binary (Dispatcher)
    agg_impl_func = compiler.compile_ir(
            typingctx,
            targetctx,
            f_ir,
            arg_typs,
            return_typ,
            compiler.DEFAULT_FLAGS,
            {}
    )

    imp_dis = numba.targets.registry.dispatcher_registry['cpu'](f)
    imp_dis.add_overload(agg_impl_func)
    return imp_dis

# adapted from numba/parfor.py
def get_parfor_reductions(parfor, parfor_params, calltypes,
                    reduce_varnames=None, param_uses=None, var_to_param=None):
    """find variables that are updated using their previous values and an array
    item accessed with parfor index, e.g. s = s+A[i]
    """
    if reduce_varnames is None:
        reduce_varnames = []

    # for each param variable, find what other variables are used to update it
    # also, keep the related nodes
    if param_uses is None:
        param_uses = defaultdict(list)
    if var_to_param is None:
        var_to_param = {}

    blocks = wrap_parfor_blocks(parfor)
    topo_order = find_topo_order(blocks)
    topo_order = topo_order[1:]  # ignore init block
    unwrap_parfor_blocks(parfor)

    for label in reversed(topo_order):
        for stmt in reversed(parfor.loop_body[label].body):
            if (isinstance(stmt, ir.Assign)
                    and (stmt.target.name in parfor_params
                        or stmt.target.name in var_to_param)):
                lhs = stmt.target.name
                rhs = stmt.value
                cur_param = lhs if lhs in parfor_params else var_to_param[lhs]
                used_vars = []
                if isinstance(rhs, ir.Var):
                    used_vars = [rhs.name]
                elif isinstance(rhs, ir.Expr):
                    used_vars = [v.name for v in stmt.value.list_vars()]
                param_uses[cur_param].extend(used_vars)
                for v in used_vars:
                    var_to_param[v] = cur_param
            if isinstance(stmt, Parfor):
                # recursive parfors can have reductions like test_prange8
                get_parfor_reductions(stmt, parfor_params, calltypes,
                    reduce_varnames, param_uses, var_to_param)

    for param, used_vars in param_uses.items():
        # a parameter is a reduction variable if its value is used to update it
        # check reduce_varnames since recursive parfors might have processed
        # param already
        if param in used_vars and param not in reduce_varnames:
            reduce_varnames.append(param)

    return reduce_varnames, var_to_param