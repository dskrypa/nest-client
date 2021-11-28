#!/usr/bin/env python

from itertools import chain
from pathlib import Path
from setuptools import setup

project_root = Path(__file__).resolve().parent

with project_root.joinpath('readme.rst').open('r', encoding='utf-8') as f:
    long_description = f.read()

about = {}
with project_root.joinpath('lib', 'nest', '__version__.py').open('r', encoding='utf-8') as f:
    exec(f.read(), about)


optional_dependencies = {
    'dev': [                                            # Development env requirements
        'ipython',
        'pre-commit',                                   # run `pre-commit install` to install hooks
    ],
    'schedule': ['bitarray'],
    'output': ['wcswidth'],                             # Not required - will use len instead if missing
}
optional_dependencies['ALL'] = sorted(set(chain.from_iterable(optional_dependencies.values())))

requirements = [
    # 'ds_tools@ git+git://github.com/dskrypa/ds_tools',
    'requests_client@ git+git://github.com/dskrypa/requests_client',
    'tz_aware_dt@ git+git://github.com/dskrypa/tz_aware_dt',
    'requests',
    'colored',
    'pyyaml',
]


setup(
    name=about['__title__'],
    version=about['__version__'],
    author=about['__author__'],
    author_email=about['__author_email__'],
    description=about['__description__'],
    long_description=long_description,
    url=about['__url__'],
    project_urls={'Source': about['__url__']},
    packages=['nest'],
    package_dir={'': 'lib'},
    classifiers=[
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
    ],
    python_requires='~=3.10',
    install_requires=requirements,
    extras_require=optional_dependencies,
    entry_points={'console_scripts': ['nest=nest.cli:main']},
)
