import setuptools
from setuptools import setup
from TSVZ import version

classifiers = [
    'Programming Language :: Python :: 3',
    'Programming Language :: Python :: 3.6',
    'Programming Language :: Python :: 3.9',
    'Programming Language :: Python :: 3.12',
    'Programming Language :: Python :: 3.14',
    'Operating System :: OS Independent',
]
# setuptools 77+ (PEP 639) takes an SPDX license expression and warns about license classifiers (an error next to an
# expression); older ones (the newest for Python 3.8 and earlier, so all a Python 3.6 install has) know only the
# free-text license and the classifier.
if int(setuptools.__version__.split('.')[0]) >= 77:
    license_metadata = {'license_expression': 'GPL-3.0-or-later', 'license_files': ['LICENSE']}
else:
    license_metadata = {'license': 'GPL-3.0-or-later'}
    classifiers.append('License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)')

setup(
    name='TSVZ',
    version=version,
    py_modules=['TSVZ'],
    description='Key-value store kept in a TSV / CSV / NSV / PSV file, with tsvz-spec-v1 support',
    author='Yufei Pan',
    author_email='pan@zopyr.us',
    url='https://github.com/tsvz-org/TSVZ',  # URL to the project’s homepage
    # The mixed-case command is a script file and not an entry point: pip 9 (stock on EL7 and EL8) reads entry_points.txt with a
    # parser that folds case, so two commands that differ only in case make it fail to upgrade or uninstall the package.
    scripts=['scripts/TSVZ'],
    entry_points={
        'console_scripts': [
            'tsvz = TSVZ:__main__',
        ],
    },
    extras_require={
        'completion': [
            'argcomplete',
        ],
    },
    classifiers=classifiers,
    python_requires='>=3.6',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    **license_metadata
)
