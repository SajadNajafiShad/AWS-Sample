from __future__ import division

import numpy as np

from . import common_args
from ..util import scale_samples, read_param_file, nonuniform_scale_samples


def sample(problem, N, seed=None):
    """Generate model inputs using Latin hypercube sampling (LHS).

    Returns a NumPy matrix containing the model inputs generated by Latin
    hypercube sampling.  The resulting matrix contains N rows and D columns,
    where D is the number of parameters.

    Parameters
    ----------
    problem : dict
        The problem definition
    N : int
        The number of samples to generate
    """
    if seed:
        np.random.seed(seed)
    D = problem['num_vars']

    result = np.empty([N, D])
    temp = np.empty([N])
    d = 1.0 / N

    for i in range(D):
        for j in range(N):
            temp[j] = np.random.uniform(low=j * d, 
                                        high=(j + 1) * d)

        np.random.shuffle(temp)

        for j in range(N):
            result[j, i] = temp[j]

    if not problem.get('dists'):
        scale_samples(result, problem['bounds'])
        return result
    else:
        scaled_latin = nonuniform_scale_samples(
            result, problem['bounds'], problem['dists'])
        return scaled_latin


# No additional CLI options
cli_parse = None


def cli_action(args):
    """Run sampling method

    Parameters
    ----------
    args : argparse namespace
    """
    problem = read_param_file(args.paramfile)
    param_values = sample(problem, args.samples, seed=args.seed)
    np.savetxt(args.output, param_values, delimiter=args.delimiter,
               fmt='%.' + str(args.precision) + 'e')


if __name__ == "__main__":
    cli_parse = None  # No additional options
    common_args.run_cli(cli_parse, cli_action)
