#!/usr/bin/env python3

from sklearn.gaussian_process.kernels import *
from sklearn import neighbors
from scipy.sparse import issparse
from scipy.spatial import distance_matrix, ConvexHull

from functools import reduce
from collections import defaultdict
from itertools import product

import warnings

import numpy as np
import pandas as pd
import scanpy as sc

import matplotlib.cm as cm
import matplotlib
import bokeh


from interactive_plotting.utils._utils import sample_unif, sample_density, to_hex_palette
from bokeh.plotting import figure, show, save as bokeh_save
from bokeh.models import ColumnDataSource, Slider, HoverTool, ColorBar, \
        Patches, Legend, CustomJS, TextInput, LabelSet, Select 
from bokeh.models.ranges import Range1d
from bokeh.models.mappers import CategoricalColorMapper, LinearColorMapper 
from bokeh.layouts import layout, column, row, GridSpec
from bokeh.transform import linear_cmap, factor_mark, factor_cmap
from bokeh.core.enums import MarkerType
from bokeh.palettes import Set1, Set2, Set3, inferno, viridis
from bokeh.models.widgets.buttons import Button

_bokeh_version = tuple(map(int, bokeh.__version__.split('.')))


_inter_hist_js_code="""
    // here is where original data is stored
    var x = orig.data['values'];

    x = x.sort((a, b) => a - b);
    var n_bins = parseInt(bins.value); // can be either string or int
    var bin_size = (x[x.length - 1] - x[0]) / n_bins;

    var hist = new Array(n_bins).fill().map((_, i) => { return 0; });
    var l_edges = new Array(n_bins).fill().map((_, i) => { return x[0] + bin_size * i; });
    var r_edges = new Array(n_bins).fill().map((_, i) => { return x[0] + bin_size * (i + 1); });
    var indices = new Array(n_bins).fill().map((_) => { return []; });

    // create the histogram
    for (var i = 0; i < x.length; i++) {
        for (var j = 0; j < r_edges.length; j++) {
            if (x[i] <= r_edges[j]) {
                hist[j] += 1;
                indices[j].push(i);
                break;
            }
        }
    }

    // make it a density
    var sum = hist.reduce((a, b) => a + b, 0);
    var deltas = r_edges.map((c, i) => { return c - l_edges[i]; });
    // just like in numpy
    hist = hist.map((c, i) => { return c / deltas[i] / sum; });

    source.data['hist'] = hist;
    source.data['l_edges'] = l_edges;
    source.data['r_edges'] = r_edges;
    source.data['indices'] = indices;

    source.change.emit();
"""


def _inter_color_code(*colors):
    assert len(colors) > 0, 'Doesn\'t make sense using no colors.'
    color_code = '\n'.join((f'renderers[i].glyph.{c} = {{field: cb_obj.value, transform: transform}};'
                            for c in colors))
    return f"""
            var transform = cmaps[cb_obj.value]['transform'];
            var low = Math.min.apply(Math, source.data[cb_obj.value]);
            var high = Math.max.apply(Math, source.data[cb_obj.value]);

            for (var i = 0; i < renderers.length; i++) {{
                {color_code}
            }}

            color_bar.color_mapper.low = low;
            color_bar.color_mapper.high = high;
            color_bar.color_mapper.palette = transform.palette;

            source.change.emit();
            """


def _set_plot_wh(fig, w, h):
    if w is not None:
        fig.plot_width = w
    if h is not None:
        fig.plot_height = h


def _create_mapper(adata, key):
    """
    Helper function to create CategoricalColorMapper from annotated data.

    Params
    --------
        adata: AnnData
            annotated data object
        key: str
            key in `adata.obs.obs_keys()` or `adata.var_names`, for which we want the colors; if no colors for given
            column are found in `adata.uns[key_colors]`, use `viridis` palette

    Returns
    --------
        mapper: bokeh.models.mappers.CategoricalColorMapper
            mapper which maps valuems from `adata.obs[key]` to colors
    """
    if not key in adata.obs_keys():
        assert key in adata.var_names,  f'`{key}` not found in `adata.obs_keys()` or `adata.var_names`'
        ix = np.where(adata.var_names == key)[0][0]
        vals = list(adata.X[:, ix])
        palette = cm.get_cmap('viridis', adata.n_obs)

        mapper = dict(zip(vals, range(len(vals))))
        palette = to_hex_palette(palette([mapper[v] for v in vals]))

        return LinearColorMapper(palette=palette, low=np.min(vals), high=np.max(vals))

    is_categorical = adata.obs[key].dtype.name == 'category'
    default_palette = cm.get_cmap('viridis', adata.n_obs if not is_categorical else len(adata.obs[key].unique()))
    palette = adata.uns.get(f'{key}_colors', default_palette)

    if palette is default_palette:
        vals = adata.obs[key].unique() if is_categorical else adata.obs[key]
        mapper = dict(zip(vals, range(len(vals))))
        palette = palette([mapper[v] for v in vals])

    palette = to_hex_palette(palette)

    if is_categorical:
        return CategoricalColorMapper(palette=palette, factors=list(map(str, adata.obs[key].cat.categories)))

    return LinearColorMapper(palette=palette, low=np.min(adata.obs[key]), high=np.max(adata.obs[key]))


def _smooth_expression(x, y, n_points=100, time_span=[None, None], mode='gp', kernel_params=dict(), kernel_default_params=dict(),
                       kernel_expr=None, default=False, verbose=False, **opt_params):
    """Smooth out the expression of given values.

    Params
    --------
    x: list(number)
        list of features
    y: list(number)
        list of targets
    n_points: int, optional (default: `100`)
        number of points to extrapolate
    time_span: list(int), optional (default `[None, None]`)
        initial and final start values for range
    mode: str, optional (default: `'gp'`)
        which regressor to use, available (`'gp'`: Gaussian Process, `'krr'`: Kernel Ridge Regression)
    kernel_params: dict, optional (default: `dict()`)
        dictionary of kernels with their parameters, keys correspond to variable names
        which can be later combined using  `kernel_expr`. Supported kernels: `ConstantKernel`, `WhiteKernel`,
        `RBF`, `Mattern`, `RationalQuadratic`, `ExpSineSquared`, `DotProduct`, `PairWiseKernel`.
    kernel_default_params: dict, optional (default: `dict()`)
        default parameters for a kernel, if not found in `kernel_params`
    kernel_expr: str, default (`None`)
        expression to combine kernel variables specified in `kernel_params`. Supported operators are `+`, `*`, `**`;
        example: kernel_expr=`'(a + b) ** 2'`, kernel_params=`{'a': ConstantKernel(1), 'b': DotProduct(2)}`
    default: bool, optional (default: `False`)
        whether to use default kernel (RBF), if none specified and/or to use default
        parameters for kernel variables in` kernel_expr`, not found in `kernel_params`
        if False, throws an Exception
    verbose: bool, optional (default: `False`)
        be more verbose
    **opt_params: kwargs
        keyword arguments for optimizer

    Returns
    --------
    x_test: np.array
        points for which we predict the values
    x_mean: np.array
        mean of the response
    cov: np.array (`None` for mode=`'krr'`)
        covariance matrix of the response
    """

    from sklearn.kernel_ridge import KernelRidge
    from sklearn.gaussian_process import GaussianProcessRegressor
    import operator as op
    import ast

    def _eval(node):
        if isinstance(node, ast.Num):
            return node.n

        if isinstance(node, ast.Name):
            if not default and node.id not in kernel_params:
                raise ValueError(f'Error while parsing `{kernel_expr}`: `{node.id}` is not a valid key in kernel_params. To use RBF kernel with default parameters, specify default=True.')
            params = kernel_params.get(node.id, kernel_default_params)
            kernel_type = params.pop('type', 'rbf')
            return kernels[kernel_type](**params)

        if isinstance(node, ast.BinOp):
            return operators[type(node.op)](_eval(node.left), _eval(node.right))

        if isinstance(node, ast.UnaryOp):
            return operators[type(node.op)](_eval(node.operand))

        raise TypeError(node)

    operators = {ast.Add : op.add,
                 ast.Mult: op.mul,
                 ast.Pow :op.pow}
    kernels = dict(const=ConstantKernel,
                   white=WhiteKernel,
                   rbf=RBF,
                   mat=Matern,
                   rq=RationalQuadratic,
                   esn=ExpSineSquared,
                   dp=DotProduct,
                   pw=PairwiseKernel)

    minn, maxx = time_span
    x_test = np.linspace(np.min(x) if minn is None else minn, np.max(x) if maxx is None else maxx, n_points)[:, None]

    if mode == 'krr':
        gamma = opt_params.pop('gamma', None)

        if gamma is None:
            length_scale = kernel_default_params.get('length_scale', 0.2)
            gamma = 1 / (2 * length_scale ** 2)
            if verbose:
                print(f'Smoothing using KRR with length_scale: {length_scale}.')

        kernel = opt_params.pop('kernel', 'rbf')
        model = KernelRidge(gamma=gamma, kernel=kernel, **opt_params)
        model.fit(x, y)

        return x_test, model.predict(x_test), [None] * n_points

    if mode == 'gp':

        if kernel_expr is None:
            assert len(kernel_params) == 1
            kernel_expr, = kernel_params.keys()

        kernel = _eval(ast.parse(kernel_expr, mode='eval').body)
        alpha = opt_params.pop('alpha', None)
        if alpha is None:
            alpha = np.std(y) 

        optimizer = opt_params.pop('optimizer', None)
        opt_params['kernel'] = kernel

        model = GaussianProcessRegressor(alpha=alpha, optimizer=optimizer, **opt_params)
        model.fit(x, y)

        mean, cov = model.predict(x_test, return_cov=True)

        return x_test, mean, cov

    raise ValueError(f'Uknown type: `{type}`.')


def _create_gt_fig(adatas, dataframe, color_key, title, color_mapper, show_cont_annot=False,
                   use_raw=True, genes=[], legend_loc='top_right',
                   plot_width=None, plot_height=None):
    """
    Helper function which create a figure with smoothed velocities, including
    confidence intervals, if possible.

    Params:
    --------
    dataframe: pandas.DataFrame
        dataframe containing the velocity data
    color_key: str
        column in `dataframe` that is to be mapped to colors
    title: str
        title of the figure
    color_mapper: bokeh.models.mappers.CategoricalColorMapper
        transformation which assings a value from `dataframe[color_key]` to a color
    show_cont_annot: bool, optional (default: `False`)
        show continuous annotations in `adata.obs`, if `color_key` is
        itself a continuous variable
    use_raw: bool, optional (default: `True`)
        whether to use adata.raw to get the expression
    genes: list, optional (default: `[]`)
        list of possible genes to color in,
        only works if `color_key` is continuous variable
    legend_loc: str, default(`'top_right'`)
        position of the legend
    plot_width: int, optional (default: `None`)
        width of the plot
    plot_height: int, optional (default: `None`)
        height of the plot

    Returns:
    --------
    fig: bokeh.plotting.figure
        figure containing the plot
    """
    
    # these markers are nearly indistinguishble
    markers = [marker for marker in MarkerType if marker not in ['circle_cross', 'circle_x']]
    fig = figure(title=title)
    _set_plot_wh(fig, plot_width, plot_height)

    renderers, color_selects = [], []
    for i, (adata, marker, (path, df)) in enumerate(zip(adatas, markers, dataframe.iterrows())):
        ds = {'dpt': df['dpt'],
              'expr': df['expr'],
              f'{color_key}': df[color_key]}
        is_categorical = color_key in adata.obs_keys() and adata.obs[color_key].dtype.name == 'category'
        if not is_categorical:
            ds, mappers = _get_mappers(adata, ds, genes, use_raw=use_raw)

        source = ColumnDataSource(ds)
        renderers.append(fig.scatter('dpt', 'expr', source=source,
                                     color={'field': color_key, 'transform': color_mapper if is_categorical else mappers[color_key]['transform']},
                                     fill_color={'field': color_key, 'transform': color_mapper if is_categorical else mappers[color_key]['transform']},
                                     line_color={'field': color_key, 'transform': color_mapper if is_categorical else mappers[color_key]['transform']},
                                     marker=marker, size=10, legend_label=path, muted_alpha=0))

        fig.xaxis.axis_label = 'dpt'
        fig.yaxis.axis_label = 'expression'
        if legend_loc is not None:
            fig.legend.location = legend_loc

        if not is_categorical and show_cont_annot:
            color_selects.append(_add_color_select(color_key, fig, [renderers[-1]], source, mappers, suffix=f' [{path}]'))

        ds = dict(df[['x_test', 'x_mean', 'x_cov']])
        if ds.get('x_test') is not None:
            if ds.get('x_mean') is not None:
                source = ColumnDataSource(ds)
                fig.line('x_test', 'x_mean', source=source, muted_alpha=0, legend_label=path)
                if all(map(lambda val: val is not None, ds.get('x_cov', [None]))):
                    x_mean = ds['x_mean']
                    x_cov = ds['x_cov']
                    band_x = np.append(ds['x_test'][::-1], ds['x_test'])
                    # black magic, known only to the most illustrious of wizards
                    band_y = np.append((x_mean - np.sqrt(np.diag(x_cov)))[::-1], (x_mean + np.sqrt(np.diag(x_cov))))
                    fig.patch(band_x, band_y, alpha=0.1, line_color='black', fill_color='black',
                              legend_label=path, line_dash='dotdash', muted_alpha=0)

            if ds.get('x_grad') is not None:
                fig.line('x_test', 'x_grad', source=source, muted_alpha=0)

    fig.legend.click_policy = 'mute'

    return column(fig, *color_selects)


def interactive_hist(adata, keys=['n_counts', 'n_genes'],
                     bins='auto',  max_bins=100,
                     groups=None, fill_alpha=0.4,
                     palette=None, display_all=True,
                     tools='pan, reset, wheel_zoom, save',
                     legend_loc='top_right',
                     plot_width=None, plot_height=None, save=None,
                     *args, **kwargs):
    """Utility function to plot distributions with variable number of bins.

    Params
    --------
    adata: AnnData object
        annotated data object
    keys: list(str), optional (default: `['n_counts', 'n_genes']`)
        keys in `adata.obs` or `adata.var` where the distibutions are stored
    bins: int; str, optional (default: `auto`)
        number of bins used for plotting or str from numpy.histogram
    max_bins: int, optional (default: `1000`)
        maximum number of bins possible
    groups: list(str), (default: `None`)
        keys in `adata.obs.obs_keys()`, groups by all possible combinations of values, e.g. for
        3 plates and 2 time points, we would create total of 6 groups
    fill_alpha: float[0.0, 1.0], (default: `0.4`)
        alpha channel of the fill color
    palette: list(str), optional (default: `None`)
        palette to use
    display_all: bool, optional (default: `True`)
        display the statistics for all data
    tools: str, optional (default: `'pan,reset, wheel_zoom, save'`)
        palette of interactive tools for the user
    legend_loc: str, (default: `'top_right'`)
        position of the legend
    legend_loc: str, default(`'top_left'`)
        position of the legend
    plot_width: int, optional (default: `None`)
        width of the plot
    plot_height: int, optional (default: `None`)
        height of the plot
    save: Union[os.PathLike, Str, NoneType], optional (default: `None`)
        path where to save the plot
    *args, **kwargs: arguments, keyword arguments
        addition argument to bokeh.models.figure

    Returns
    --------
    None
    """

    if max_bins < 1:
        raise ValueError(f'`max_bins` must >= 1')

    palette = Set1[9] + Set2[8] + Set3[12] if palette is None else palette

    # check the input
    for key in keys:
        if key not in adata.obs.keys() and \
           key not in adata.var.keys() and \
           key not in adata.var_names:
            raise ValueError(f'The key `{key}` does not exist in `adata.obs`, `adata.var` or `adata.var_names`.')

    def _create_adata_groups():
        if groups is None:
            return [adata], [('all',)]

        combs = list(product(*[set(adata.obs[g]) for g in groups]))
        adatas= [adata[reduce(lambda l, r: l & r,
                              (adata.obs[k] == v for k, v in zip(groups, vals)), True)]
                 for vals in combs] + [adata]

        if display_all:
            combs += [('all',)]
            adatas += [adata]

        return adatas, combs

    # group_v_combs contains the value combinations
    ad_gs = _create_adata_groups()
    
    cols = []
    for key in keys:
        callbacks = []
        fig = figure(*args, tools=tools, **kwargs)
        slider = Slider(start=1, end=max_bins, value=0, step=1,
                        title='Bins')

        plots = []
        for j, (ad, group_vs) in enumerate(filter(lambda ad_g: ad_g[0].n_obs > 0, zip(*ad_gs))):

            if key in ad.obs.keys():
                orig = ad.obs[key]
                hist, edges = np.histogram(orig, density=True, bins=bins)
            elif key in ad.var.keys():
                orig = ad.var[key]
                hist, edges = np.histogram(orig, density=True, bins=bins)
            else:
                orig = ad[:, key].X
                hist, edges = np.histogram(orig, density=True, bins=bins)

            slider.value = len(hist)
            # case when automatic bins
            max_bins = max(max_bins, slider.value)

            # original data, used for recalculation of histogram in JS code
            orig = ColumnDataSource(data=dict(values=orig))
            # data that we update in JS code
            source = ColumnDataSource(data=dict(hist=hist, l_edges=edges[:-1], r_edges=edges[1:]))

            legend = ', '.join(': '.join(map(str, gv)) for gv in zip(groups, group_vs)) \
                    if groups is not None else 'all'
            p = fig.quad(source=source, top='hist', bottom=0,
                         left='l_edges', right='r_edges',
                         fill_color=palette[j], legend_label=legend if legend_loc is not None else None,
                         muted_alpha=0,
                         line_color="#555555", fill_alpha=fill_alpha)

            # create callback and slider
            callback = CustomJS(args=dict(source=source, orig=orig), code=_inter_hist_js_code)
            callback.args['bins'] = slider
            callbacks.append(callback)

            # add the current plot so that we can set it
            # visible/invisible in JS code
            plots.append(p)

        slider.end = max_bins

        # slider now updates all values
        slider.js_on_change('value', *callbacks)

        button = Button(label='Toggle', button_type='primary')
        button.callback = CustomJS(
            args={'plots': plots},
            code='''
                 for (var i = 0; i < plots.length; i++) {
                     plots[i].muted = !plots[i].muted;
                 }
                 '''
        )

        if legend_loc is not None:
            fig.legend.location = legend_loc
            fig.legend.click_policy = 'mute'

        fig.xaxis.axis_label = key
        fig.yaxis.axis_label = 'normalized frequency'
        _set_plot_wh(fig, plot_width, plot_height)

        cols.append(column(slider, button, fig))

    if _bokeh_version > (1, 0, 4):
        from bokeh.layouts import grid
        plot = grid(children=cols, ncols=2)
    else:
        cols = list(map(list, np.array_split(cols, np.ceil(len(cols) / 2))))
        plot = layout(children=cols, sizing_mode='fixed', ncols=2)

    if save is not None:
        save = save if str(save).endswith('.html') else str(save) + '.html'
        bokeh_save(plot, save)
    else:
        show(plot)


def thresholding_hist(adata, key, categories, basis=['umap'], components=[1, 2],
                      bins='auto', palette=None, legend_loc='top_right',
                      plot_width=None, plot_height=None, save=None):
    """Histogram with the option to highlight categories based on thresholding binned values.

    Params
    --------
    adata: AnnData object
        annotated data object
    key: str
        key in `adata.obs_keys()` where the data is stored
    categories: dict
        dictionary with keys corresponding to group names and values to starting boundaries `[min, max]`
    basis: list, optional (default: `['umap']`)
        basis in `adata.obsm_keys()` to visualize
    components: list(int); list(list(int)), optional (default: `[1, 2]`)
        components to use for each basis
    bins: int; str, optional (default: `auto`)
        number of bins used for initial binning or a string key used in from numpy.histogram
    palette: list(str), optional (default: `None`)
         palette to use for coloring categories
    legend_loc: str, default(`'top_right'`)
        position of the legend
    plot_width: int, optional (default: `None`)
        width of the plot
    plot_height: int, optional (default: `None`)
        height of the plot
    save: Union[os.PathLike, Str, NoneType], optional (default: `None`)
        path where to save the plot

    Returns
    --------
    None
    """

    if not isinstance(components[0], list):
        components = [components]

    if len(components) != len(basis):
        assert len(basis) % len(components) == 0 and len(basis) >= len(components)
        components = components * (len(basis) // len(components))

    if not isinstance(components, np.ndarray):
        components = np.asarray(components)

    if not isinstance(basis, list):
        basis = [basis]

    palette = Set1[9] + Set2[8] + Set3[12] if palette is None else palette

    hist_fig = figure()
    _set_plot_wh(hist_fig, plot_width, plot_height)

    hist_fig.xaxis.axis_label = key
    hist_fig.yaxis.axis_label = 'normalized frequency'
    hist, edges = np.histogram(adata.obs[key], density=True, bins=bins)
    
    source = ColumnDataSource(data=dict(hist=hist, l_edges=edges[:-1], r_edges=edges[1:],
                              category=['default'] * len(hist), indices=[[]] * len(hist)))

    df = pd.concat([pd.DataFrame(adata.obsm[f'X_{bs}'][:, comp - (bs != 'diffmap')], columns=[f'x_{bs}', f'y_{bs}'])
                    for bs, comp in zip(basis, components)], axis=1)
    df['values'] = list(adata.obs[key])
    df['category'] = 'default'
    df['visible_category'] = 'default'
    df['cat_stack'] = [['default']] * len(df)

    orig = ColumnDataSource(df)
    color = dict(field='category', transform=CategoricalColorMapper(palette=palette, factors=list(categories.keys())))
    hist_fig.quad(source=source, top='hist', bottom=0,
                  left='l_edges', right='r_edges', color=color,
                  line_color="#555555", legend_group='category')
    if legend_loc is not None:
        hist_fig.legend.location = legend_loc

    emb_figs = []
    for bs, comp in zip(basis, components):
        fig = figure(title=bs)

        fig.xaxis.axis_label = f'{bs}_{comp[0]}'
        fig.yaxis.axis_label = f'{bs}_{comp[1]}'
        _set_plot_wh(fig, plot_width, plot_height)

        fig.scatter(f'x_{bs}', f'y_{bs}', source=orig, size=10, color=color, legend_group='category')
        if legend_loc is not None:
            fig.legend.location = legend_loc

        emb_figs.append(fig)

    inputs, category_cbs = [], []
    code_start, code_mid, code_thresh = [], [], []
    args = {'source': source, 'orig': orig}

    for col, cat_item in zip(palette, categories.items()):
        cat, (start, end) = cat_item
        inp_min = TextInput(name='test', value=f'{start}', title=f'{cat}/min')
        inp_max = TextInput(name='test', value=f'{end}', title=f'{cat}/max')

        code_start.append(f'''
            var min_{cat} = parseFloat(inp_min_{cat}.value);
            var max_{cat} = parseFloat(inp_max_{cat}.value);
        ''')
        code_mid.append(f'''
            var mid_{cat} = (source.data['r_edges'][i] - source.data['l_edges'][i]) / 2;
        ''')
        code_thresh.append(f'''
            if (source.data['l_edges'][i] + mid_{cat} >= min_{cat} && source.data['r_edges'][i] - mid_{cat} <= max_{cat}) {{
                source.data['category'][i] = '{cat}';
                for (var j = 0; j < source.data['indices'][i].length; j++) {{
                    var ix = source.data['indices'][i][j];
                    orig.data['category'][ix] = '{cat}';
                }}
            }}
        ''')
        args[f'inp_min_{cat}'] = inp_min
        args[f'inp_max_{cat}'] = inp_max
        min_ds = ColumnDataSource(dict(xs=[start] * 2))
        max_ds = ColumnDataSource(dict(xs=[end] * 2))

        inputs.extend([inp_min, inp_max])

    code_thresh.append(
    '''
        {
            source.data['category'][i] = 'default';
            for (var j = 0; j < source.data['indices'][i].length; j++) {
                var ix = source.data['indices'][i][j];
                orig.data['category'][ix] = 'default';
            }
        }
    ''')
    callback = CustomJS(args=args, code=f'''
        {';'.join(code_start)}
        for (var i = 0; i < source.data['hist'].length; i++) {{
            {';'.join(code_mid)}
            {' else '.join(code_thresh)}
        }}
        orig.change.emit();
        source.change.emit();
    ''')

    for input in inputs:
        input.js_on_change('value', callback)

    slider = Slider(start=1, end=100, value=len(hist), title='Bins')
    interactive_hist_cb = CustomJS(args={'source': source, 'orig': orig, 'bins': slider}, code=_inter_hist_js_code)
    slider.js_on_change('value', interactive_hist_cb, callback)

    plot = column(row(hist_fig, column(slider, *inputs)), *emb_figs)

    if save is not None:
        save = save if str(save).endswith('.html') else str(save) + '.html'
        bokeh_save(plot, save)
    else:
        show(plot)


def gene_trend(adata, paths, genes=None, mode='gp', exp_key='X',
               separate_paths=False, show_cont_annot=False,
               extra_genes=[], n_points=100, show_zero_counts=True,
               time_span=[None, None], use_raw=True,
               n_velocity_genes=5, length_scale=0.2,
               path_key='leiden', color_key='leiden',
               share_y=True, legend_loc='top_right',
               plot_width=None, plot_height=None, save=None, **kwargs):
    """
    Function which shows expression levels as well as velocity per gene as a function of DPT.

    Params
    --------
    adata: AnnData
        annotated data object
    paths: list(list(str))
        different paths to visualize
    genes: list, optional (default: `None`)
        list of genes to show, if `None` take `n_velocity` genes
        from `adata.var['velocity_genes']`
    mode: str, optional (default: `'gp'`)
        whether to use Kernel Ridge Regression (`'krr'`) or a Gaussian Process (`'gp'`) for
        smoothing the expression values
    exp_key: str, optional (default: `'X'`)
        key from adata.layers or just `'X'` to get expression values
    separate_paths: bool, optional (default: `False`)
        whether to show each path for each gene in a separate plot
    show_cont_annot: bool, optional (default: `False`)
        show continuous annotations in `adata.obs`,
        only works if `color_key` is continuous variable
    extra_genes: list(str), optional (default: `[]`)
        list of possible genes to color in,
        only works if `color_key` is continuous variable
    n_points: int, optional (default: `100`)
        how many points to use for the smoothing
    time_span: list(int), optional (default `[None, None]`)
        initial and final start values for range, `None` corresponds to min/max
    use_raw: bool, optional (default: `True`)
        whether to use adata.raw to get the expression
    show_zero_counts: bool, optional (default: `True`)
        whether to show cells with zero counts
    n_velocity_genes: int, optional (default: `5`)
        number of genes to take from` adata.var['velocity_genes']`
    length_scale : float, optional (default `0.2`)
        length scale for RBF kernel
    path_key: str, optional (default: `'leiden'`)
        key in `adata.obs_keys()` where to look for groups specified in `paths` argument
    color_key: str, optional (default: `'leiden'`)
        key in `adata.obs_keys()` which is color in plot
    share_y: bool, optional (default: `True`)
        whether to share y-axis when plotting paths separately
    legend_loc: str, default(`'top_right'`)
        position of the legend
    plot_width: int, optional (default: `None`)
        width of the plot
    plot_height: int, optional (default: `None`)
        height of the plot
    save: Union[os.PathLike, Str, NoneType], optional (default: `None`)
        path where to save the plot
    **kwargs: kwargs
        keyword arguments for KRR or GP

    Returns
    --------
    None
    """

    if mode == 'krr':
        warnings.warn('KRR is experimental; please consider using mode=`gp`')

    for path in paths:
        for p in path:
            assert p in adata.obs[path_key].cat.categories, f'`{p}` is not in `adata.obs[path_key]`. Possible values are: `{list(adata.obs[path_key].cat.categories)}`.'

    # check the input
    if 'dpt_pseudotime' not in adata.obs.keys():
        raise ValueError('`dpt_pseudotime` is not in `adata.obs.keys()`')

    # check the genes list
    if genes is None:
        genes = adata[:, adata.var['velocity_genes']].var_names[:n_velocity_genes]

    genes_indicator = np.in1d(genes, adata.var_names) #[gene in adata.var_names for gene in genes]
    if not all(genes_indicator):
        genes_missing = np.array(genes)[np.invert(genes_indicator)]
        print(f'Could not find the following genes: `{genes_missing}`.')
        genes = list(np.array(genes)[genes_indicator])

    mapper = _create_mapper(adata, color_key)
    figs, adatas = [], []

    for gene in genes:
        data = defaultdict(list)
        row_figs = []
        y_lim_min, y_lim_max = np.inf, -np.inf
        for path in paths:
            path_ix = np.in1d(adata.obs[path_key], path)
            ad = adata[path_ix].copy()

            minn, maxx = time_span
            ad.obs['dpt_pseudotime'] = ad.obs['dpt_pseudotime'].replace(np.inf, 1)
            minn = np.min(ad.obs['dpt_pseudotime']) if minn is None else minn
            maxx = np.max(ad.obs['dpt_pseudotime']) if maxx is None else maxx

            # wish I could get rid of this copy
            ad = ad[(ad.obs['dpt_pseudotime'] >= minn) & (ad.obs['dpt_pseudotime'] <= maxx)]

            gene_exp = ad[:, gene].layers[exp_key] if exp_key != 'X' else (ad.raw if use_raw else ad)[:, gene].X

            # exclude dropouts
            ix = (gene_exp > 0).squeeze()
            indexer = slice(None) if show_zero_counts else ix
            # just use for sanity check with asserts
            rev_indexer = ix if show_zero_counts else slice(None)

            dpt = ad.obs['dpt_pseudotime']

            if issparse(gene_exp):
                gene_exp = gene_exp.A

            gene_exp = np.squeeze(gene_exp[indexer, None])
            data['expr'].append(gene_exp)
            y_lim_min, y_lim_max = min(y_lim_min, np.min(gene_exp)), max(y_lim_max, np.max(gene_exp))

            # compute smoothed values from expression
            data['dpt'].append(np.squeeze(dpt[indexer, None]))
            data[color_key].append(np.array(ad[indexer].obs[color_key]))

            assert all(gene_exp[rev_indexer] > 0)

            if len(gene_exp[rev_indexer]) == 0:
                print(f'All counts are 0 for: `{gene}`.')
                continue

            x_test, exp_mean, exp_cov = _smooth_expression(np.expand_dims(dpt[ix], -1), gene_exp[ix if show_zero_counts else slice(None)], mode=mode,
                                                           time_span=time_span, n_points=n_points, kernel_params=dict(k=dict(length_scale=length_scale)),
                                                           **kwargs)
                                                      
            data['x_test'].append(x_test)
            data['x_mean'].append(exp_mean)
            data['x_cov'].append(exp_cov)

            # we need this for the _create mapper
            adatas.append(ad[indexer])
            
            if separate_paths:
                dataframe = pd.DataFrame(data, index=list(map(lambda path: ', '.join(map(str, path)), [path])))
                row_figs.append(_create_gt_fig(adatas, dataframe, color_key, title=gene, color_mapper=mapper,
                                               show_cont_annot=show_cont_annot, legend_loc=legend_loc, genes=extra_genes,
                                               use_raw=use_raw, plot_width=plot_width, plot_height=plot_height))
                adatas = []
                data = defaultdict(list)

        if separate_paths:
            if share_y:
                # first child is the figure
                for fig in map(lambda c: c.children[0], row_figs):
                    fig.y_range = Range1d(y_lim_min - 0.1, y_lim_max + 0.1)

            figs.append(row(row_figs))
            row_figs = []
        else:
            dataframe = pd.DataFrame(data, index=list(map(lambda path: ', '.join(map(str, path)), paths)))
            figs.append(_create_gt_fig(adatas, dataframe, color_key, title=gene, color_mapper=mapper,
                                       show_cont_annot=show_cont_annot, legend_loc=legend_loc, genes=extra_genes,
                                       use_raw=use_raw, plot_width=plot_width, plot_height=plot_height))

    plot = column(*figs)

    if save is not None:
        save = save if str(save).endswith('.html') else str(save) + '.html'
        bokeh_save(plot, save)
    else:
        show(plot)


def highlight_de(adata, basis='umap', components=[1, 2], n_top_genes=10,
                 de_keys='names, scores, pvals_adj, logfoldchanges',
                 cell_keys='', n_neighbors=5, fill_alpha=0.1, show_hull=True,
                 legend_loc='top_right', plot_width=None, plot_height=None, save=None):
    """
    Highlight differential expression by hovering over clusters.

    Params
    --------
    adata: AnnData
        annotated data object
    basis: str, optional (default: `'umap'`)
        basis used in visualization
    components: list(int), optional (default: `[1, 2]`)
        components of the basis
    n_top_genes: int, optional (default: `10`)
        number of differentially expressed genes to display
    de_keys: list(str); str, optional (default: `'names, scores, pvals_ads, logfoldchanges'`)
        list or comma-seperated values of keys in `adata.uns['rank_genes_groups'].keys()`
        to be displayed for each cluster
    cell_keys: list(str); str, optional (default: '')
        keys in `adata.obs_keys()` to be displayed
    n_neighbors: int, optional (default: `5`)
        number of neighbors for KNN classifier, which 
        controls how the convex hull looks like
    fill_alpha: float, optional (default: `0.1`)
        alpha value of the cluster colors
    show_hull: bool, optional (default: `True`)
        show the convex hull along each cluster
    legend_loc: str, default(`'top_right'`)
        position of the legend
    plot_width: int, optional (default: `None`)
        width of the plot
    plot_height: int, optional (default: `None`)
        height of the plot
    save: Union[os.PathLike, Str, NoneType], optional (default: `None`)
        path where to save the plot

    Returns
    --------
    None
    """

    if 'rank_genes_groups' not in adata.uns_keys():
        raise ValueError('Run differential expression first.')


    if isinstance(de_keys, str):
        de_keys = list(dict.fromkeys(map(str.strip, de_keys.split(','))))
        if de_keys != ['']:
            assert all(map(lambda k: k in adata.uns['rank_genes_groups'].keys(), de_keys)), 'Not all keys are in `adata.uns[\'rank_genes_groups\']`.'
        else:
            de_keys = []

    if isinstance(cell_keys, str):
        cell_keys = list(dict.fromkeys(map(str.strip, cell_keys.split(','))))
        if cell_keys != ['']:
            assert all(map(lambda k: k in adata.obs.keys(), cell_keys)), 'Not all keys are in `adata.obs.keys()`.'
        else:
            cell_keys = []

    if f'X_{basis}' not in adata.obsm.keys():
        raise ValueError(f'Key `X_{basis}` not found in adata.obsm.')

    if not isinstance(components, np.ndarray):
        components = np.asarray(components)

    key = adata.uns['rank_genes_groups']['params']['groupby']
    if key not in cell_keys:
        cell_keys.insert(0, key)

    df = pd.DataFrame(adata.obsm[f'X_{basis}'][:, components - (basis != 'diffmap')], columns=['x', 'y'])
    for k in cell_keys:
        df[k] = list(map(str, adata.obs[k]))

    knn = neighbors.KNeighborsClassifier(n_neighbors)
    knn.fit(df[['x', 'y']], adata.obs[key])
    df['prediction'] = knn.predict(df[['x', 'y']])

    conv_hulls = df[df[key] == df['prediction']].groupby(key).apply(lambda df: df.iloc[ConvexHull(np.vstack([df['x'], df['y']]).T).vertices])

    mapper = _create_mapper(adata, key)
    categories = adata.obs[key].cat.categories
    fig = figure(tools='pan, reset, wheel_zoom, lasso_select, save')
    _set_plot_wh(fig, plot_width, plot_height)
    legend_dict = defaultdict(list)

    for k in categories:
        d = df[df[key] == k]
        data_source =  ColumnDataSource(d)
        legend_dict[k].append(fig.scatter('x', 'y', source=data_source, color={'field': key, 'transform': mapper}, size=5, muted_alpha=0))

    hover_cell = HoverTool(renderers=[r[0] for r in legend_dict.values()], tooltips=[(f'{key}', f'@{key}')] + [(f'{k}', f'@{k}') for k in cell_keys[1:]])

    c_hulls = conv_hulls.copy()
    de_possible = conv_hulls[key].isin(adata.uns['rank_genes_groups']['names'].dtype.names)
    ok_patches = []
    prev_cat = []
    for i, isin in enumerate((~de_possible, de_possible)):
        conv_hulls = c_hulls[isin]

        if len(conv_hulls) == 0:
            continue

        # must use 'group' instead of key since key is MultiIndex
        conv_hulls.rename(columns={'leiden': 'group'}, inplace=True)
        xs, ys, ks = zip(*conv_hulls.groupby('group').apply(lambda df: list(map(list, (df['x'], df['y'], df['group'])))))
        tmp_data = defaultdict(list)
        tmp_data['xs'] = xs
        tmp_data['ys'] = ys
        tmp_data[key] = list(map(lambda k: k[0], ks))
        
        if i == 1:
            ix = list(map(lambda k: adata.uns['rank_genes_groups']['names'].dtype.names.index(k), tmp_data[key]))
            for k in de_keys:
                tmp = np.array(list(zip(*adata.uns['rank_genes_groups'][k])))[ix, :n_top_genes]
                for j in range(n_top_genes):
                    tmp_data[f'{k}_{j}'] = tmp[:, j]

        tmp_data = pd.DataFrame(tmp_data)
        for k in categories:
            d = tmp_data[tmp_data[key] == k]
            source = ColumnDataSource(d)

            patches = fig.patches('xs', 'ys', source=source, fill_alpha=fill_alpha, muted_alpha=0, hover_alpha=0.5,
                                  color={'field': key, 'transform': mapper} if (show_hull and i == 1) else None,
                                  hover_color={'field': key, 'transform': mapper} if (show_hull and i == 1) else None)
            legend_dict[k].append(patches)
            if i == 1:
                ok_patches.append(patches)

    hover_group = HoverTool(renderers=ok_patches, tooltips=[(f'{key}', f'@{key}'),
        ('groupby', adata.uns['rank_genes_groups']['params']['groupby']),
        ('reference', adata.uns['rank_genes_groups']['params']['reference']),
        ('rank', ' | '.join(de_keys))] + [(f'#{i + 1}', ' | '.join((f'@{k}_{i}' for k in de_keys))) for i in range(n_top_genes)]
    )
    

    fig.toolbar.active_inspect = [hover_group]
    if len(cell_keys) > 1:
        fig.add_tools(hover_group, hover_cell)
    else:
        fig.add_tools(hover_group)

    if legend_loc is not None:
        legend = Legend(items=list(legend_dict.items()), location=legend_loc)
        fig.add_layout(legend)
        fig.legend.click_policy = 'hide'  # hide does disable hovering, whereas 'mute' does not

    fig.xaxis.axis_label = f'{basis}_{components[0]}'
    fig.yaxis.axis_label = f'{basis}_{components[1]}'

    if save is not None:
        save = save if str(save).endswith('.html') else str(save) + '.html'
        bokeh_save(fig, save)
    else:
        show(fig)


def link_plot(adata, key, genes=None, basis=['umap', 'pca'], components=[1, 2],
             subsample=None, steps=[40, 40], sample_size=500,
             distance=2, cutoff=True, highlight_only=None, palette=None,
             show_legend=False, legend_loc='top_right', plot_width=None, plot_height=None, save=None):
    """
    Display the distances of cells from currently highlighted cell.

    Params
    --------
    adata: AnnData
        annotated data object
    key: str 
        key in `adata.obs_keys()` to color the static plot
    genes: list(str), optional (default: `None`)
        list of genes in `adata.var_names`,
        which are used to compute the distance;
        if None, take all the genes
    basis: list(str), optional (default:`['umap', 'pca']`)
        list of basis to use when plotting;
        only the first plot is hoverable
    components: list(int); list(list(int)), optional (default: `[1, 2]`)
        list of components for each basis
    subsample: str, optional (default: `None`)
        subsample strategy to use when there are too many cells
        possible values are: `"density"`, `"uniform"`, `None`
    steps: int; list(int), optional (default: `[40, 40]`)
        number of steps in each direction when using `subsample="uniform"`
    sample_size: int, optional (default: `500`)
        number of cells to sample based on their density in the respective embedding
        when using `subsample="density"`; should be < `1000`
    distance: int; str, optional (default: `2`)
        for integers, use p-norm,
        for strings, only `'dpt'` is available
    cutoff: bool, optional (default: `True`)
        if `True`, do not color cells whose distance is further away
        than the threshold specified by the slider
    highlight_only: 'str', optional (default: `None`)
        key in `adata.obs_keys()`, which makes highlighting
        work only on clusters specified by this parameter
    palette: matplotlib.colors.Colormap; list(str), optional (default: `None`)
        colormap to use, if None, use plt.cm.RdYlBu 
    show_legend: bool, optional (default: `False`)
        display the legend also in the linked plot
    legend_loc: str, optional (default `'top_right'`)
        location of the legend
    seed: int, optional (default: `None`)
        seed when `subsample='density'`
    plot_width: int, optional (default: `None`)
        width of the plot
    plot_height: int, optional (default: `None`)
        height of the plot
    save: Union[os.PathLike, Str, NoneType], optional (default: `None`)
        path where to save the plot

    Returns
    --------
    None
    """

    assert key in adata.obs.keys(), f'`{key}` not found in `adata.obs`.'

    if subsample == 'uniform':
        adata, _ = sample_unif(adata, steps, basis[0])
    elif subsample == 'density':
        adata, _ = sample_density(adata, sample_size, basis[0], seed=seed)
    elif subsample is not None:
        raise ValueError(f'Unknown subsample strategy: `{subsample}`.')

    palette = cm.RdYlBu if palette is None else palette
    if isinstance(palette, matplotlib.colors.Colormap):
        palette = to_hex_palette(palette(range(palette.N), 1., bytes=True))

    if not isinstance(components[0], list):
        components = [components]

    if len(components) != len(basis):
        assert len(basis) % len(components) == 0 and len(basis) >= len(components)
        components = components * (len(basis) // len(components))

    if not isinstance(components, np.ndarray):
        components = np.asarray(components)

    if highlight_only is not None:
        assert highlight_only in adata.obs_keys(), f'`{highlight_only}` is not in adata.obs_keys().'

    genes = adata.var_names if genes is None else genes 
    gene_subset = np.in1d(adata.var_names, genes)

    if distance != 'dpt':
        d = adata.X[:, gene_subset]
        if issparse(d):
            d = d.A
        dmat = distance_matrix(d, d, p=distance)
    else:
        if not all(gene_subset):
            warnings.warn('`genes` is not None, are you sure this is what you want when using `dpt` distance?')

        dmat = []
        ad_tmp = adata.copy()
        ad_tmp = ad_tmp[:, gene_subset]
        for i in range(ad_tmp.n_obs):
            ad_tmp.uns['iroot'] = i
            sc.tl.dpt(ad_tmp)
            dmat.append(list(ad_tmp.obs['dpt_pseudotime'].replace([np.nan, np.inf], [0, 1])))

    dmat = pd.DataFrame(dmat, columns=list(map(str, range(adata.n_obs))))
    df = pd.concat([pd.DataFrame(adata.obsm[f'X_{bs}'][:, comp - (bs != 'diffmap')], columns=[f'x{i}', f'y{i}'])
                    for i, (bs, comp) in enumerate(zip(basis, components))] + [dmat], axis=1)
    df['hl_color'] = np.nan
    df['index'] = range(len(df))
    df['hl_key'] = list(adata.obs[highlight_only]) if highlight_only is not None else 0
    df[key] = list(map(str, adata.obs[key]))

    start_ix = '0'  # our root cell
    ds = ColumnDataSource(df)
    mapper = linear_cmap(field_name='hl_color', palette=palette,
                         low=df[start_ix].min(), high=df[start_ix].max())
    static_fig_mapper = _create_mapper(adata, key)

    static_figs = []
    figs, renderers = [], []
    for i, bs in enumerate(basis):
        # linked plots
        fig = figure(tools='pan, reset, save, ' + ('zoom_in, zoom_out' if i == 0 else 'wheel_zoom'),
                     title=bs, plot_width=400, plot_height=400)
        _set_plot_wh(fig, plot_width, plot_height)

        kwargs = {}
        if show_legend and legend_loc is not None:
            kwargs['legend_group'] = 'hl_key' if highlight_only is not None else key

        scatter = fig.scatter(f'x{i}', f'y{i}', source=ds, line_color=mapper, color=mapper,
                              hover_color='black', size=8, line_width=8, line_alpha=0, **kwargs)
        if show_legend and legend_loc is not None:
            fig.legend.location = legend_loc

        figs.append(fig)
        renderers.append(scatter)
    
        # static plots
        fig = figure(title=bs, plot_width=400, plot_height=400)

        fig.scatter(f'x{i}', f'y{i}', source=ds, size=8,
                    color={'field': key, 'transform': static_fig_mapper}, **kwargs)

        if legend_loc is not None:
            fig.legend.location = legend_loc
    
        static_figs.append(fig)

    fig = figs[0]

    end = dmat[~np.isinf(dmat)].max().max() if distance != 'dpt' else 1.0
    slider = Slider(start=0, end=end, value=end / 2, step=end / 1000,
                    title='Distance ' + '(dpt)' if distance == 'dpt' else f'({distance}-norm)')
    col_ds = ColumnDataSource(dict(value=[start_ix]))
    update_color_code = f'''
        source.data['hl_color'] = source.data[first].map(
            (x, i) => {{ return isNaN(x) ||
                        {'x > slider.value || ' if cutoff else ''}
                        source.data['hl_key'][first] != source.data['hl_key'][i]  ? NaN : x; }}
        );
    '''
    slider.callback = CustomJS(args={'slider': slider, 'mapper': mapper['transform'], 'source': ds, 'col': col_ds}, code=f'''
        mapper.high = slider.value;
        var first = col.data['value'];
        {update_color_code}
        source.change.emit();
    ''')

    h_tool = HoverTool(renderers=renderers, tooltips=[], show_arrow=False)
    h_tool.callback = CustomJS(args=dict(source=ds, slider=slider, col=col_ds), code=f'''
        var indices = cb_data.index['1d'].indices;
        if (indices.length == 0) {{
            source.data['hl_color'] = source.data['hl_color'];
        }} else {{
            var first = indices[0];
            source.data['hl_color'] = source.data[first];
            {update_color_code}
            col.data['value'] = first;
            col.change.emit();
        }}
        source.change.emit();
    ''')
    fig.add_tools(h_tool)

    color_bar = ColorBar(color_mapper=mapper['transform'], width=12, location=(0,0))
    fig.add_layout(color_bar, 'left')

    fig.add_tools(h_tool)
    plot = column(slider, row(*static_figs), row(*figs))

    if save is not None:
        save = save if str(save).endswith('.html') else str(save) + '.html'
        bokeh_save(plot, save)
    else:
        show(plot)


def _get_mappers(adata, df, genes=[], use_raw=True, sort=True):
    if sort:
        genes = sorted(genes)

    mappers = {c:{'field': c, 'transform': _create_mapper(adata, c)}
               for c in (sorted if sort else list)(filter(lambda c: adata.obs[c].dtype.name != 'category', adata.obs.columns)) + genes}

    # assume all columns in .obs are numbers
    for k in filter(lambda k: k not in genes, mappers.keys()):
        df[k] = list(adata.obs[k].astype(float))

    indices, = np.where(np.in1d(adata.var_names, genes))
    for ix in indices:
        df[adata.var_names[ix]] = (adata.raw if use_raw else adata).X[:, ix]

    return df, mappers


def _add_color_select(key, fig, renderers, source, mappers, colors=['color', 'fill_color', 'line_color'],
                      color_bar_pos='right', suffix=''):
    color_bar = ColorBar(color_mapper=mappers[key]['transform'], width=10, location=(0, 0))
    fig.add_layout(color_bar, color_bar_pos)

    code = _inter_color_code(*colors)
    callback= CustomJS(args=dict(renderers=renderers, source=source, color_bar=color_bar, cmaps=mappers),
                       code=code)

    return Select(title=f'Select variable to color{suffix}:', value=key,
                  options=list(mappers.keys()), callback=callback)
