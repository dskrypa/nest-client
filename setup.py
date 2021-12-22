#!/usr/bin/env python

from itertools import chain
from pathlib import Path
from setuptools import setup, find_packages

project_root = Path(__file__).resolve().parent
long_description = project_root.joinpath('readme.rst').read_text('utf-8')
about = {}
exec(project_root.joinpath('lib', 'nest_client', '__version__.py').read_text('utf-8'), about)

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
    'requests_client@ git+git://github.com/dskrypa/requests_client',
    'httpx',
    'colored',
    'pyyaml',
    'tzdata',
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
    packages=find_packages('lib'),
    package_dir={'': 'lib'},
    classifiers=[
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
    ],
    python_requires='~=3.9',
    install_requires=requirements,
    extras_require=optional_dependencies,
    entry_points={'console_scripts': ['nest=nest_client.cli:main']},
)
