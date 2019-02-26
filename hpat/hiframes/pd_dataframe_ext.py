import operator
import pandas as pd
import numpy as np
import numba
from numba import types, cgutils
from numba.extending import (models, register_model, lower_cast, infer_getattr,
    type_callable, infer, overload, make_attribute_wrapper, intrinsic,
    lower_builtin)
from numba.typing.templates import (infer_global, AbstractTemplate, signature,
    AttributeTemplate, bound_function)
from numba.targets.imputils import impl_ret_new_ref, impl_ret_borrowed
from numba.typing.arraydecl import (get_array_index_type, _expand_integer,
    ArrayAttribute, SetItemBuffer)
from numba.typing.npydecl import (Numpy_rules_ufunc, NumpyRulesArrayOperator,
    NumpyRulesInplaceArrayOperator, NumpyRulesUnaryArrayOperator,
    NdConstructorLike)
import hpat
from hpat.hiframes.pd_series_ext import SeriesType
from hpat.str_ext import string_type, list_string_array_type
from hpat.str_arr_ext import (string_array_type, offset_typ, char_typ,
    str_arr_payload_type, StringArrayType, GetItemStringArray)
from hpat.hiframes.pd_timestamp_ext import pandas_timestamp_type, datetime_date_type
from hpat.hiframes.pd_categorical_ext import PDCategoricalDtype, get_categories_int_type
from hpat.hiframes.rolling import supported_rolling_funcs
import datetime


class DataFrameType(types.Type):  # TODO: IterableType over column names
    """Temporary type class for DataFrame objects.
    """
    def __init__(self, data=None, index=None, columns=None, has_parent=False):
        # data is tuple of Array types
        # index is Array type (TODO: Index obj)
        # columns is tuple of strings

        self.data = data
        if index is None:
            index = types.none
        self.index = index
        self.columns = columns
        # keeping whether it is unboxed from Python to enable reflection of new
        # columns
        self.has_parent = has_parent
        super(DataFrameType, self).__init__(
            name="dataframe({}, {}, {}, {})".format(
                data, index, columns, has_parent))

    def copy(self):
        # XXX is copy necessary?
        index = types.none if self.index == types.none else self.index.copy()
        data = tuple(a.copy() for a in self.data)
        return DataFrameType(data, index, self.columns, self.has_parent)

    @property
    def key(self):
        # needed?
        return self.data, self.index, self.columns, self.has_parent

    def unify(self, typingctx, other):
        if (isinstance(other, DataFrameType)
                and len(other.data) == len(self.data)
                and other.columns == self.columns
                and other.has_parent == self.has_parent):
            new_index = types.none
            if self.index != types.none and other.index != types.none:
                new_index = self.index.unify(typingctx, other.index)
            elif other.index != types.none:
                new_index = other.index
            elif self.index != types.none:
                new_index = self.index

            data = tuple(a.unify(typingctx, b) for a,b in zip(self.data, other.data))
            return DataFrameType(
                data, new_index, self.columns, self.has_parent)

    def can_convert_to(self, typingctx, other):
        return
        # overload resolution tries to convert for even get_dataframe_data()
        # TODO: find valid conversion possibilities
        # if (isinstance(other, DataFrameType)
        #         and len(other.data) == len(self.data)
        #         and other.columns == self.columns):
        #     import pdb; pdb.set_trace()
        #     data_convert = max(a.can_convert_to(typingctx, b)
        #                         for a,b in zip(self.data, other.data))
        #     if self.index == types.none and other.index == types.none:
        #         return data_convert
        #     if self.index != types.none and other.index != types.none:
        #         return max(data_convert,
        #             self.index.can_convert_to(typingctx, other.index))

    def is_precise(self):
        return all(a.is_precise() for a in self.data) and self.index.is_precise()


# TODO: encapsulate in meminfo since dataframe is mutible, for example:
# df = pd.DataFrame({'A': A})
# df2 = df
# if cond:
#    df['A'] = B
# df2.A
# TODO: meminfo for reference counting of dataframes
@register_model(DataFrameType)
class DataFrameModel(models.StructModel):
    def __init__(self, dmm, fe_type):
        n_cols = len(fe_type.columns)
        members = [
            ('data', types.Tuple(fe_type.data)),
            ('index', fe_type.index),
            ('columns', types.UniTuple(string_type, n_cols)),
            # for lazy unboxing of df coming from Python (usually argument)
            # list of flags noting which columns and index are unboxed
            # index flag is last
            ('unboxed', types.UniTuple(types.int8, n_cols + 1)),
            ('parent', types.pyobject),
        ]
        super(DataFrameModel, self).__init__(dmm, fe_type, members)

make_attribute_wrapper(DataFrameType, 'data', '_data')
make_attribute_wrapper(DataFrameType, 'index', '_index')
make_attribute_wrapper(DataFrameType, 'columns', '_columns')
make_attribute_wrapper(DataFrameType, 'unboxed', '_unboxed')
make_attribute_wrapper(DataFrameType, 'parent', '_parent')


@infer_getattr
class DataFrameAttribute(AttributeTemplate):
    key = DataFrameType

    def resolve_iat(self, ary):
        return DataFrameIatType(ary)

    def resolve_iloc(self, ary):
        return DataFrameILocType(ary)

    def resolve_loc(self, ary):
        return DataFrameLocType(ary)

    def resolve_values(self, ary):
        # using np.stack(data, 1) for both typing and implementation
        stack_sig = self.context.resolve_function_type(
            np.stack, (types.Tuple(ary.data), types.IntegerLiteral(1)), {})
        return stack_sig.return_type

    def generic_resolve(self, df, attr):
        ind = df.columns.index(attr)
        arr_typ = df.data[ind]
        return SeriesType(arr_typ.dtype, arr_typ, df.index, True)


@intrinsic
def init_dataframe(typingctx, *args):
    """Create a DataFrame with provided data, index and columns values.
    Used as a single constructor for DataFrame and assigning its data, so that
    optimization passes can look for init_dataframe() to see if underlying
    data has changed, and get the array variables from init_dataframe() args if
    not changed.
    """

    n_cols = len(args)//2
    data_typs = tuple(args[:n_cols])
    index_typ = args[n_cols]
    column_names = tuple(a.literal_value for a in args[n_cols+1:])

    def codegen(context, builder, signature, args):
        in_tup = args[0]
        data_arrs = [builder.extract_value(in_tup, i) for i in range(n_cols)]
        index = builder.extract_value(in_tup, n_cols)
        column_strs = [numba.unicode.make_string_from_constant(
                    context, builder, string_type, c) for c in column_names]
        # create dataframe struct and store values
        dataframe = cgutils.create_struct_proxy(
            signature.return_type)(context, builder)

        data_tup = context.make_tuple(
            builder, types.Tuple(data_typs), data_arrs)
        column_tup = context.make_tuple(
            builder, types.UniTuple(string_type, n_cols), column_strs)
        zero = context.get_constant(types.int8, 0)
        unboxed_tup = context.make_tuple(
            builder, types.UniTuple(types.int8, n_cols+1), [zero]*(n_cols+1))

        dataframe.data = data_tup
        dataframe.index = index
        dataframe.columns = column_tup
        dataframe.unboxed = unboxed_tup
        dataframe.parent = context.get_constant_null(types.pyobject)

        # increase refcount of stored values
        if context.enable_nrt:
            context.nrt.incref(builder, index_typ, index)
            for var, typ in zip(data_arrs, data_typs):
                context.nrt.incref(builder, typ, var)
            for var in column_strs:
                context.nrt.incref(builder, string_type, var)

        return dataframe._getvalue()

    ret_typ = DataFrameType(data_typs, index_typ, column_names)
    sig = signature(ret_typ, types.Tuple(args))
    return sig, codegen


@intrinsic
def has_parent(typingctx, df=None):
    def codegen(context, builder, sig, args):
        dataframe = cgutils.create_struct_proxy(
            sig.args[0])(context, builder, value=args[0])
        return cgutils.is_not_null(builder, dataframe.parent)
    return signature(types.bool_, df), codegen


# TODO: alias analysis
# this function should be used for getting df._data for alias analysis to work
# no_cpython_wrapper since Array(DatetimeDate) cannot be boxed
@numba.generated_jit(nopython=True, no_cpython_wrapper=True)
def get_dataframe_data(df, i):

    def _impl(df, i):
        if has_parent(df) and df._unboxed[i] == 0:
            # TODO: make df refcounted to avoid repeated unboxing
            df = hpat.hiframes.boxing.unbox_dataframe_column(df, i)
        return df._data[i]

    return _impl


# TODO: use separate index type instead of just storing array
@numba.generated_jit(nopython=True, no_cpython_wrapper=True)
def get_dataframe_index(df):
    return lambda df: df._index


@intrinsic
def set_df_column_with_reflect(typingctx, df, cname, arr):
    """Set df column and reflect to parent Python object
    return a new df.
    """

    col_name = cname.literal_value
    n_cols = len(df.columns)
    new_n_cols = n_cols
    data_typs = df.data
    column_names = df.columns
    index_typ = df.index
    is_new_col = col_name not in df.columns
    col_ind = n_cols
    if is_new_col:
        data_typs += (arr,)
        column_names += (col_name,)
        new_n_cols += 1
    else:
        col_ind = df.columns.index(col_name)
        data_typs = tuple((arr if i == col_ind else data_typs[i])
                          for i in range(n_cols))

    def codegen(context, builder, signature, args):
        df_arg, _, arr_arg = args

        in_dataframe = cgutils.create_struct_proxy(df)(
            context, builder, value=df_arg)

        data_arrs = [builder.extract_value(in_dataframe.data, i)
                    if i != col_ind else arr_arg for i in range(n_cols)]
        if is_new_col:
            data_arrs.append(arr_arg)

        column_strs = [numba.unicode.make_string_from_constant(
                    context, builder, string_type, c) for c in column_names]

        unboxed_vals = [builder.extract_value(in_dataframe.unboxed, i)
                        if i != col_ind else arr_arg for i in range(n_cols)]
        zero = context.get_constant(types.int8, 0)
        one = context.get_constant(types.int8, 1)
        if unboxed_vals:
            unboxed_vals.append(one)  # for new data array
        unboxed_vals.append(zero)  # for index

        index = in_dataframe.index
        # create dataframe struct and store values
        out_dataframe = cgutils.create_struct_proxy(
            signature.return_type)(context, builder)

        data_tup = context.make_tuple(
            builder, types.Tuple(data_typs), data_arrs)
        column_tup = context.make_tuple(
            builder, types.UniTuple(string_type, new_n_cols), column_strs)
        unboxed_tup = context.make_tuple(
            builder, types.UniTuple(types.int8, new_n_cols+1), unboxed_vals)

        out_dataframe.data = data_tup
        out_dataframe.index = index
        out_dataframe.columns = column_tup
        out_dataframe.unboxed = unboxed_tup
        out_dataframe.parent = in_dataframe.parent  # TODO: refcount of parent?

        # increase refcount of stored values
        if context.enable_nrt:
            context.nrt.incref(builder, index_typ, index)
            for var, typ in zip(data_arrs, data_typs):
                context.nrt.incref(builder, typ, var)
            for var in column_strs:
                context.nrt.incref(builder, string_type, var)

        # set column of parent
        # get boxed array
        pyapi = context.get_python_api(builder)
        gil_state = pyapi.gil_ensure()  # acquire GIL
        env_manager = context.get_env_manager(builder)

        if context.enable_nrt:
            context.nrt.incref(builder, arr, arr_arg)
        py_arr = pyapi.from_native_value(arr, arr_arg, env_manager)    # calls boxing

        # get column as string obj
        cstr = context.insert_const_string(builder.module, col_name)
        cstr_obj = pyapi.string_from_string(cstr)

        # set column array
        pyapi.object_setitem(in_dataframe.parent, cstr_obj, py_arr)

        pyapi.decref(py_arr)
        pyapi.decref(cstr_obj)

        pyapi.gil_release(gil_state)    # release GIL

        return out_dataframe._getvalue()

    ret_typ = DataFrameType(data_typs, index_typ, column_names, True)
    sig = signature(ret_typ, df, cname, arr)
    return sig, codegen


@overload(len)  # TODO: avoid lowering?
def df_len_overload(df):
    if len(df.columns) == 0:  # empty df
        return lambda df: 0
    return lambda df: len(df._data[0])


@overload(operator.getitem)  # TODO: avoid lowering?
def df_getitem_overload(df, ind):
    if isinstance(df, DataFrameType) and isinstance(ind, types.StringLiteral):
        index = df.columns.index(ind.literal_value)
        return lambda df, ind: hpat.hiframes.api.init_series(df._data[index])


@infer_global(operator.getitem)
class GetItemDataFrame(AbstractTemplate):
    key = operator.getitem

    def generic(self, args, kws):
        df, idx = args
        # df1 = df[df.A > .5]
        if (isinstance(df, DataFrameType)
                and isinstance(idx, (SeriesType, types.Array))
                and idx.dtype == types.bool_):
            return signature(df, *args)


@infer
class StaticGetItemDataFrame(AbstractTemplate):
    key = "static_getitem"

    def generic(self, args, kws):
        df, idx = args
        if (isinstance(df, DataFrameType) and isinstance(idx, list)
                and all(isinstance(c, str) for c in idx)):
            data_typs = tuple(df.data[df.columns.index(c)] for c in idx)
            columns = tuple(idx)
            ret_typ = DataFrameType(data_typs, df.index, columns)
            return signature(ret_typ, *args)


# handle getitem for Tuples because sometimes df._data[i] in
# get_dataframe_data() doesn't translate to 'static_getitem' which causes
# Numba to fail. See TestDataFrame.test_unbox1, TODO: find root cause in Numba
# adapted from typing/builtins.py
@infer_global(operator.getitem)
class GetItemTuple(AbstractTemplate):
    key = operator.getitem

    def generic(self, args, kws):
        tup, idx = args
        if (not isinstance(tup, types.BaseTuple) or
                not isinstance(idx, types.IntegerLiteral)):
            return
        idx_val = idx.literal_value
        if isinstance(idx_val, int):
            ret = tup.types[idx_val]
        elif isinstance(idx_val, slice):
            ret = types.BaseTuple.from_types(tup.types[idx_val])

        return signature(ret, *args)


# adapted from targets/tupleobj.py
@lower_builtin(operator.getitem, types.BaseTuple, types.IntegerLiteral)
@lower_builtin(operator.getitem, types.BaseTuple, types.SliceLiteral)
def getitem_tuple_lower(context, builder, sig, args):
    tupty, idx = sig.args
    idx = idx.literal_value
    tup, _ = args
    if isinstance(idx, int):
        if idx < 0:
            idx += len(tupty)
        if not 0 <= idx < len(tupty):
            raise IndexError("cannot index at %d in %s" % (idx, tupty))
        res = builder.extract_value(tup, idx)
    elif isinstance(idx, slice):
        items = cgutils.unpack_tuple(builder, tup)[idx]
        res = context.make_tuple(builder, sig.return_type, items)
    else:
        raise NotImplementedError("unexpected index %r for %s"
                                  % (idx, sig.args[0]))
    return impl_ret_borrowed(context, builder, sig.return_type, res)


# TODO: handle dataframe pass
# df.ia[] type
class DataFrameIatType(types.Type):
    def __init__(self, df_type):
        self.df_type = df_type
        name = "DataFrameIatType({})".format(df_type)
        super(DataFrameIatType, self).__init__(name)

# df.iloc[] type
class DataFrameILocType(types.Type):
    def __init__(self, df_type):
        self.df_type = df_type
        name = "DataFrameILocType({})".format(df_type)
        super(DataFrameILocType, self).__init__(name)

# df.loc[] type
class DataFrameLocType(types.Type):
    def __init__(self, df_type):
        self.df_type = df_type
        name = "DataFrameLocType({})".format(df_type)
        super(DataFrameLocType, self).__init__(name)


@infer
class StaticGetItemDataFrameIat(AbstractTemplate):
    key = 'static_getitem'

    def generic(self, args, kws):
        df, idx = args
        # TODO: handle df.at[]
        if isinstance(df, DataFrameIatType):
            # df.iat[3,1]
            if (isinstance(idx, tuple) and len(idx) == 2
                    and isinstance(idx[0], int)
                    and isinstance(idx[1], int)):
                col_no = idx[1]
                data_typ = df.df_type.data[col_no]
                return signature(data_typ.dtype, *args)

@infer_global(operator.getitem)
class GetItemDataFrameIat(AbstractTemplate):
    key = operator.getitem

    def generic(self, args, kws):
        df, idx = args
        # TODO: handle df.at[]
        if isinstance(df, DataFrameIatType):
            # df.iat[n,1]
            if (isinstance(idx, types.Tuple) and len(idx) == 2
                    and isinstance(idx.types[1], types.IntegerLiteral)):
                col_no = idx.types[1].literal_value
                data_typ = df.df_type.data[col_no]
                return signature(data_typ.dtype, *args)

@infer_global(operator.setitem)
class SetItemDataFrameIat(AbstractTemplate):
    key = operator.setitem

    def generic(self, args, kws):
        df, idx, val = args
        # TODO: handle df.at[]
        if isinstance(df, DataFrameIatType):
            # df.iat[n,1] = 3
            if (isinstance(idx, types.Tuple) and len(idx) == 2
                    and isinstance(idx.types[1], types.IntegerLiteral)):
                col_no = idx.types[1].literal_value
                data_typ = df.df_type.data[col_no]
                return signature(types.none, data_typ, idx.types[0], val)


@infer_global(operator.getitem)
class GetItemDataFrameLoc(AbstractTemplate):
    key = operator.getitem

    def generic(self, args, kws):
        df, idx = args
        # handling df.loc similar to df.iloc as temporary hack
        # TODO: handle proper labeled indexes
        if isinstance(df, DataFrameLocType):
            # df1 = df.loc[df.A > .5], df1 = df.loc[np.array([1,2,3])]
            if (isinstance(idx, (SeriesType, types.Array, types.List))
                    and (idx.dtype == types.bool_
                        or isinstance(idx.dtype, types.Integer))):
                return signature(df.df_type, *args)
            # df.loc[1:n]
            if isinstance(idx, types.SliceType):
                return signature(df.df_type, *args)
            # df.loc[1:n,'A']
            if (isinstance(idx, types.Tuple) and len(idx) == 2
                    and isinstance(idx.types[1], types.StringLiteral)):
                col_name = idx.types[1].literal_value
                col_no = df.df_type.columns.index(col_name)
                data_typ = df.df_type.data[col_no]
                # TODO: index
                ret_typ = SeriesType(data_typ.dtype, None, True)
                return signature(ret_typ, *args)


@infer_global(operator.getitem)
class GetItemDataFrameILoc(AbstractTemplate):
    key = operator.getitem

    def generic(self, args, kws):
        df, idx = args
        if isinstance(df, DataFrameILocType):
            # df1 = df.iloc[df.A > .5], df1 = df.iloc[np.array([1,2,3])]
            if (isinstance(idx, (SeriesType, types.Array, types.List))
                    and (idx.dtype == types.bool_
                        or isinstance(idx.dtype, types.Integer))):
                return signature(df.df_type, *args)
            # df.iloc[1:n]
            if isinstance(idx, types.SliceType):
                return signature(df.df_type, *args)
            # df.iloc[1:n,0]
            if (isinstance(idx, types.Tuple) and len(idx) == 2
                    and isinstance(idx.types[1], types.IntegerLiteral)):
                col_no = idx.types[1].literal_value
                data_typ = df.df_type.data[col_no]
                # TODO: index
                ret_typ = SeriesType(data_typ.dtype, None, True)
                return signature(ret_typ, *args)