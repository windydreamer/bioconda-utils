"""
Recipe Linter

QC checks (linter) for recipes, returning a TSV of issues identified.

The strategy here is to use simple functions that do a single check on
a recipe. When run on a single recipe it can be used for linting new
contributions; when run on all recipes it helps highlight entire classes of
problems to be addressed.

See the `lint_functions` module for these.

After writing the function, register it in the global ``registry`` dict,
``lint_functions.registry``.

The output is a TSV where the "info" column contains the dicts returned by
each check function, and this column is expanded into multiple extra colums.
While this results in a lot of NaNs, it makes it easy to drop non-interesting
cases with pandas, e.g.,

.. code:: python

   recipes_with_missing_tests = df.dropna(subset=['no_tests'])

or

.. code:: python

    def not_in_bioconda(x):
        if not isinstance(x, set):
            return np.nan
        res = set(x).difference(['bioconda'])
        if len(res):
            return(res)
        return np.nan

    df['other'] = df.exists_in_channel.apply(not_in_bioconda)
    other_channels = df[['recipe', 'other']].dropna()


TODO:
~~~~~

- check version and build number against master branch. I think there's stuff
  in bioconductor updating to handle this sort of thing. Also bioconda_utils
  has utils for checking against master branch.

  - if version changed, ensure build number is 0
  - if version unchanged, ensure build number incremented

- currently we don't pay attention to py27/py3. It would be nice to handle
  that.

- how to define valid licenses?
  (conda_build.metadata.ensure_valid_license_family is for family)

- gcc/llvm have their respective preprocessing selectors

- excessive comments (from skeletons?)

"""

import os
import re
import itertools
from collections import defaultdict, namedtuple
from typing import List

import pandas as pd
import numpy as np
import ruamel_yaml as yaml

from . import utils
from . import lint_functions
from .recipe import Recipe, RecipeError

import logging
logger = logging.getLogger(__name__)



usage = """
Perform various checks on recipes.
"""

def select_recipes(packages, git_range, recipe_folder, config_filename, config, force):
    if git_range:
        modified = utils.modified_recipes(git_range, recipe_folder, config_filename)
        if not modified:
            logger.info('No recipe modified according to git, exiting.')
            return []

        # Recipes with changed `meta.yaml` or `build.sh` files
        changed_recipes = [
            os.path.dirname(f) for f in modified
            if os.path.basename(f) in ['meta.yaml', 'build.sh'] and
            os.path.exists(f)
        ]
        logger.info(
            'Recipes to consider according to git: \n{}'.format(
                '\n '.join(changed_recipes)))
    else:
        changed_recipes = []

    blacklisted_recipes = utils.get_blacklist(config['blacklists'], recipe_folder)

    selected_recipes = list(utils.get_recipes(recipe_folder, packages))
    _recipes = []
    for recipe in selected_recipes:
        stripped = os.path.relpath(recipe, recipe_folder)
        if stripped in blacklisted_recipes and recipe in changed_recipes:
            logger.warning('%s is blacklisted but also has changed. Consider '
                           'removing from blacklist if you want to build it', recipe)
        if force:
            _recipes.append(recipe)
            logger.debug('forced: %s', recipe)
            continue
        if stripped in blacklisted_recipes:
            logger.debug('blacklisted: %s', recipe)
            continue
        if git_range:
            if recipe not in changed_recipes:
                continue
        _recipes.append(recipe)
        logger.debug(recipe)

    logger.info('Recipes to lint:\n{}'.format('\n '.join(_recipes)))
    return _recipes


class LintArgs(namedtuple('LintArgs', (
    'exclude', 'registry',
))):
    """
    exclude : list
        List of function names in ``registry`` to skip globally. When running on
        CI, this will be merged with anything else detected from the commit
        message or LINT_SKIP environment variable using the special string
        "[skip lint <function name> for <recipe name>]". While those other
        mechanisms define skipping on a recipe-specific basis, this argument
        can be used to skip tests for all recipes. Use sparingly.

    registry : list or tuple
        List of functions to apply to each recipe. If None, defaults to
        `bioconda_utils.lint_functions.registry`.
    """
    def __new__(cls, exclude=None, registry=None):
        return super().__new__(cls, exclude, registry)


def lint(recipes: List[str], lint_args, basedir="recipes"):
    """
    Parameters
    ----------

    recipes : list
        List of recipes to lint

    lint_args : LintArgs
    """
    exclude = lint_args.exclude
    registry = lint_args.registry

    if registry is None:
        registry = lint_functions.registry

    skip_dict = defaultdict(list)

    commit_message = ""
    if 'LINT_SKIP' in os.environ:
        # Allow overwriting of commit message
        commit_message = os.environ['LINT_SKIP']
    else:
        # Obtain commit message from last commit.
        commit_message = utils.run(
            ['git', 'log', '--format=%B', '-n', '1'], mask=False
        ).stdout

    # For example the following text in the commit message will skip
    # lint_functions.uses_setuptools for recipe argparse:
    #
    # [ lint skip uses_setuptools for argparse ]

    skip_re = re.compile(
        r'\[\s*lint skip (?P<func>\w+) for (?P<recipe>.*?)\s*\]')
    to_skip = skip_re.findall(commit_message)

    if exclude is not None:
        # exclude arg is used to skip test for *all* packages
        to_skip += list(itertools.product(exclude, recipes))

    for func, recipe in to_skip:
        skip_dict[recipe].append(func)

    hits = []
    for recipe in sorted(recipes):
        logger.debug("Linting: %s", recipe)

        try:
            recipe_obj = Recipe.from_file(basedir, recipe)
        except RecipeError as exc:
            result = {'load_recipe':  str(exc), 'fix': str(exc)}
            line = getattr(exc, 'line', None)
            if line is not None:
                result['start_line'] = result['end_line'] = line
            hits.append({
                'recipe': recipe,
                'check': 'load_recipe',
                'severity': 'ERROR',
                'info': result
            })
            continue

        # Since lint functions need a parsed meta.yaml, checking for parsing
        # errors can't be a lint function.
        #
        # TODO: do we need a way to skip this the same way we can skip lint
        # functions? I can't think of a reason we'd want to keep an unparseable
        # YAML.
        metas = []
        try:
            for platform in ["linux", "osx"]:
                config = utils.load_conda_build_config(platform=platform, trim_skip=False)
                metas.extend(utils.load_all_meta(recipe, config=config, finalize=False))
        except (
                yaml.scanner.ScannerError, yaml.constructor.ConstructorError, SystemExit
        ) as exc:
            hits.append({
                'recipe': recipe,
                'check': 'parse_error',
                'severity': 'ERROR',
                'info': {'parse_error': str(exc), 'fix': str(exc), '_exc': type(exc),
                         'test1': getattr(exc, 'code', None), 'test2': exc.args }
            })
            continue

        # skips defined in commit message
        skip_for_this_recipe = set(skip_dict[recipe])

        # skips defined in meta.yaml
        for meta in metas:
            persistent = meta.get_value('extra/skip-lints', [])
            skip_for_this_recipe.update(persistent)

        for func in registry:
            if func.__name__ in skip_for_this_recipe:
                skip_sources = [
                    ('Commit message', skip_dict[recipe]),
                    ('skip-lints', persistent),
                ]
                for source, skips in skip_sources:
                    if func.__name__ not in skips:
                        continue
                    logger.info('%s defines skip lint test %s for recipe %s',
                                source, func.__name__, recipe)
                continue
            result = func(recipe_obj, metas)
            if result:
                hits.append(
                    {'recipe': recipe,
                     'check': func.__name__,
                     'info': result})

    if hits:
        report = pd.DataFrame(hits)[['recipe', 'check', 'info']]

        # expand out the info into more columns
        info = pd.DataFrame(list(report['info'].values))
        report = pd.concat((report, info), axis=1)
        return report
    else:
        return


def markdown_report(report=None):
    """
    Return a rendered Markdown string.

    Parameters
    ----------
    report : None or pandas.DataFrame
        If None, linting assumed to be successful. If dataframe, it's provided
        to the lint failure template.
    """
    if report is None:
        tmpl = utils.jinja.get_template("lint_success.md")
        return tmpl.render()

    if 'check' in report:
        # we have the unsummarized report
        report = pd.DataFrame(dict(
            failed_tests=report.groupby('recipe')['check'].agg('unique')
        ))

    tmpl = utils.jinja.get_template("lint_failure.md")
    return tmpl.render(report=report)
