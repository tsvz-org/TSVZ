from setuptools import setup
from TSVZ import version

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
    classifiers=[
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.12',
        'Programming Language :: Python :: 3.14',
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Operating System :: OS Independent',
    ],
    python_requires='>=3.6',
    long_description=open('README.md', encoding='utf-8').read(),
    long_description_content_type='text/markdown',
    license='GPLv3+',
)
