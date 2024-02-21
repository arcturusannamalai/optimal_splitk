import numpy as np
import numba
from tqdm import tqdm
from .encode import encode_model, encode_design, decode_design
from .init import initialize_single
from .optim.doptim import Doptim
from .utils import CACHE

##################################################################                
##  UPDATE FORMULAS
##################################################################

@numba.njit(cache=CACHE)
def x2fx(Y, model):
    """
    Create the model matrix from the design matrix and model specification.
    This specification is the same as MATLAB's.
    A model is specified as a matrix with each term being a row. The elements
    in each row specify the power of the factor.
    E.g.

    * The intercept is [0, 0, ..., 0]
    * A main factor is [1, 0, ..., 0]
    * A two-factor interaction is [1, 1, 0, ..., 0]
    * A quadratic term is [2, 0, ..., 0]

    .. note::
        This function is Numba accelerated

    Parameters
    ----------
    Y : np.array
        The design matrix. It should be 2D
    model : np.array
        The model, specified as in MATLAB.

    Returns
    -------
    X : np.array
        The model matrix
    """
    X = np.zeros((*Y.shape[:-1], model.shape[0]))
    for i, term in enumerate(model):
        p = np.ones(Y.shape[:-1])
        for j in range(model.shape[1]):
            if term[j] != 0:
                if term[j] == 1:
                    p *= Y[..., j]
                else:
                    p *= Y[..., j] ** term[j]
        X[..., i] = p
    return X

##################################################################
##  OPTIMIZATION
##################################################################

@numba.njit(cache=CACHE)
def generate_coordinates(cat_lvl, default=np.array([[]], dtype=np.float64)):
    """
    Generate possible coordinates depending on the amount of
    categorical levels. 1 is considered continuous, higher means
    categorical with n levels.

    Currently, the design only generates [-1, 0, 1] for continuous variables
    and all possible effect encoded values for categorical variables.

    .. note::
        This function is Numba accelerated

    Parameters
    ----------
    cat_lvl : int
        The amount of categorical levels, with 1 indicating a
        continuous factor
    default : np.array(2d)
        The coordinates to be outputted instead of generating new
        possible coordinates

    Returns
    -------
    coords : np.array(2d)
        The array of possible coordinates (each item
        along axis 0 is a corodinate).
    """
    if default.size == 0:
        # Check categorical or continuous column
        if cat_lvl <= 1:
            possible_coords = np.array([[-1], [0], [1]], dtype=np.float64)
        else:
            n_cat_levels = cat_lvl - 1
            possible_coords = np.concatenate((np.eye(n_cat_levels), -np.ones((1, n_cat_levels))))
    else:
        # Store default coordinates
        possible_coords = default

    return possible_coords

# not compilable with numba v0.59.0
#@numba.njit(cache=CACHE)
def optimize(Y, model, plot_sizes, factors,
             optim:object, prestate, max_it=10, col_start=None, default_coords=None):
    """
    Optimize a model iteratively using the coordinate exchange algorithm.

    .. note::
        This function is Numba accelerated

    Parameters
    ----------
    Y : np.array    
        The initial design matrix (usually randomized) to optimize
    model : np.array    
        The regression model to optimize the design for. Encoded
        as in MATLAB.
    plot_sizes : np.array
        The size of each plot in the split-plot constraints. The first
        element are the easy-to-vary effects.
    factors : np.array
        Information on the columns of the design matrix. It is encoded
        as a 2d array with the first element being the split-plot-level,
        and the second element being the type (continuous = 1, categorical > 1).
    optim : :py:class:`optimal_splitk.optimizers.Optim`
        A optimization object specifying the different functions related
        to an optimization criterion (like D-optimality by default)
    prestate : `Prestate`
        The pre-computed state return from the optim.prestate function. This allows
        some caching related to the specific metric.
    max_it : int
        The maximum amount of iterations for the algorithm, if at one iteration,
        no update is performed, the algorithm is ended earlier.
    col_start : np.array(1d)
        Contains the starting column of each effect.
        Possibly pre-computed start of each column, this is necessary
        when working with categorical factors or mixture components.
    default_coords : list(np.array(2d))
        Contains possible default coordinates for all the different
        factors. If None, a default set will be generated by 
        :py:func:`generate_coordinates` 

    Returns
    -------
    Y : np.array(2d)
        The final design matrix
    metric : np.array(1d)
        The final metric of the design
    """
    ##################################################
    # INITIALIZATION
    ##################################################
    # Compute model matrix
    X = x2fx(Y, model)

    # State initialization
    state = optim.init(prestate, Y, X)

    # Compute betas
    alphas = np.cumprod(plot_sizes[::-1])[::-1]
    betas = np.cumprod(np.concatenate((np.array([1]), plot_sizes)))

    # Start column of each factor
    if col_start is None:
        col_start = np.concatenate((np.array([0]), 
                                    np.cumsum(np.where(factors[:, 1] > 1, 
                                                       factors[:, 1] - 1, 
                                                       np.ones(factors.shape[0], dtype=np.int64)))))

    # Compute possible coordinates for each level
    if default_coords is not None:
        _possible_coords = [generate_coordinates(cat_lvl, dcoord) for dcoord, (_, cat_lvl) in zip(default_coords, factors)]
    else:
        _possible_coords = [generate_coordinates(cat_lvl) for _, cat_lvl in factors]

    # Make sure we are not stuck in finite loop
    for it in range(max_it):
        # Start with updated false
        updated = False

        ##################################################
        # FACTOR SELECTION
        ##################################################

        # Loop over all factors
        for i, factor in enumerate(factors):
            # Level in split-plot
            level, cat_lvl = factor[0], factor[1]
            jmp = betas[level]
            col = col_start[i]

            # Loop over all run-groups
            for grp in range(alphas[level]):

                ##################################################
                # COORDINATE GENERATION
                ##################################################
                # Generate coordinates
                possible_coords = _possible_coords[i]
                cols = slice(col, col + possible_coords.shape[1])
                runs = slice(grp*jmp, (grp+1)*jmp)

                # Extract current coordinate (as best)
                init_coord = np.copy(Y[runs.start, cols])
                best_coord = init_coord

                # Loop over possible new coordinates
                for new_coord in possible_coords:
                    # Set new coordinate
                    Y[runs, cols] = new_coord

                    # Validate whether to check the coordinate
                    if not np.all(new_coord == init_coord):
                        ##################################################
                        # COMPUTE UPDATE
                        ##################################################
                        # Compute the model matrix of the update
                        Xi_star = x2fx(Y[runs], model)

                        # Protection against singularity errors
                        try:
                            # Compute the update
                            accept, state = optim.update(state, X, Xi_star, level, grp)
                        except Exception:
                            accept = False
                        
                        ##################################################
                        # ACCEPT UPDATE
                        ##################################################
                        # New best design
                        if accept:
                            # Store the best coordinates
                            best_coord = new_coord
                            # Update X (model matrix)
                            X[runs] = Xi_star
                            # Set update
                            updated = True
                
                # Set the best coordinates
                Y[runs, cols] = best_coord
        
        # Stop if nothing updated for an entire iteration
        if not updated:
            break
        else:
            state.Minv[:] = np.linalg.inv(X.T @ np.linalg.solve(state.V, X))

    # Compute the metric
    metric = optim.metric(state, Y, X)

    return Y, metric

##################################################################
##  DOE WRAPPER
##################################################################

def doe(model, plot_sizes, factors, n_tries=10, max_it=10000, 
        it_callback=None, optim=Doptim, default_coords=None, ratios=None):
    """
    Create a D-optimal design of experiments (DOE) using the coordinate exchange algorithm.
    This is the core function of the library.

    Parameters
    ----------
    model : np.array    
        The regression model to optimize the design for. Encoded
        as in MATLAB.
    plot_sizes : np.array
        The size of each plot in the split-plot constraints. The first
        element are the easy-to-vary effects.
    factors : np.array
        Information on the columns of the design matrix. It is encoded
        as a 2d array with the first element being the split-plot-level,
        and the second element being the type (continuous = 1, categorical > 1).
    optim : :py:class:`optimal_splitk.optimizers.Optim`
        A optimization object specifying the different functions related
        to an optimization criterion (like D-optimality by default)
    n_tries : int
        The amount of random starts of the coordinate exchange algorithm
    max_it : int
        The maximum amount of full iterations per optimization.
    it_callback : function(int)
        Called each iteration (for external updating progress bars)
    optim : :py:class:`optimal_splitk.optimizers.Optim`
        A optimization object specifying the different functions related
        to an optimization criterion (like D-optimality by default)
    default_coords : list(np.array(2d))
        Contains possible default coordinates for all the different
        factors. If None, a default set will be generated by 
        :py:func:`generate_coordinates` 
    ratios : np.array(1d)
        The ratios for each split-level. The size should be the same as
        or 1 less than the amount of plot sizes. If the same, the first
        element should be 1 (to indicate a 1 ratio for epsilon).

    Returns
    -------
    Y : np.array
        The best found design
    determinants: np.array
        An array of the determinants of :math:`|X^T V^{-1} X|`
    """
    ##################################################
    # INITIALIZATION
    ##################################################
    # Encode the model
    model_enc = encode_model(model, factors)

    # Create empty design
    Y = np.zeros((np.prod(plot_sizes), factors.shape[0]))

    # Set iteration callback
    if it_callback is None:
        it_callback = (lambda x: None)

    # Store determinants
    metrics = np.zeros(n_tries)
    best_metric = -np.inf
    best_Y = Y

    # Initialize ratios
    if ratios is None:
        ratios = np.ones_like(plot_sizes, dtype=np.float64)
    elif ratios.size + 1 == plot_sizes.size:
        ratios = np.concatenate(np.array([1]), ratios)
    else:
        assert ratios.size == plot_sizes.size, 'Ratio sizes do not match plot sizes'
        assert ratios[0] == 1, 'First element of ratios should be 1'

    # Compute pre-state
    prestate = optim.preinit(plot_sizes, (model, model_enc), factors, ratios)

    # Try multiple random starts
    with tqdm(total=n_tries) as pbar:
        i = 0
        while i < n_tries:
            ##################################################
            # DESIGN CREATION
            ##################################################
            # Initialize random design and encode it
            Yo = initialize_single(plot_sizes, factors, Y, coords=default_coords)
            Yoenc = encode_design(Yo, factors)

            ##################################################
            # OPTIMIZATION
            ##################################################
            try:
                Yo, metric = optimize(Yoenc, model_enc, plot_sizes, factors, 
                                    optim, prestate, max_it=max_it, default_coords=default_coords)

                # Store the results
                metrics[i] = metric
                if metric > best_metric:
                    best_metric = metric
                    best_Y = np.copy(Yo)

                # Update the progress
                i += 1
                pbar.update(1)
                it_callback(i)
            except np.linalg.LinAlgError:
                pass

    # Decode the optimal design
    best_Y = decode_design(best_Y, factors)     

    return best_Y, metrics






