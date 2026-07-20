### Reference: Repo2Run

I.1 Pipreqs settings
Figure 11 shows the template of a Dockerfile generated using “requirements_pipreqs.txt” created by pipreqs.
```bash
FROM python:3.10
WORKDIR /
RUN apt-get update && apt-get install -y curl && \\
curl -sSL https://install.python-poetry.org | python -
ENV PATH="/root/.local/bin:$PATH"
RUN pip install pytest pytest-xdist && \\
pip install pipdeptree && \\
git clone https://github.com/{author_name}/{package_name}.git && \\
mkdir /repo && \\
git config --global --add safe.directory /repo && \\
cp -r {package_name}/. /repo && rm -rf {package_name}/ && \\
rm -rf {package_name}
COPY requirements_pipreqs.txt /requirements_pipreqs.txt
RUN pip install -r /requirements_pipreqs.txt
RUN cd /repo && pytest --collect-only -q
```

### Reference: Pipreqs README.rst
pipreqs - Generate requirements.txt file for any project based on imports
  https://codecov.io/gh/bndr/pipreqs/branch/master/graph/badge.svg?token=0rfPfUZEAX 
Installation
pip install pipreqs
Obs.: if you don't want support for jupyter notebooks, you can install pipreqs without the dependencies that give support to it. To do so, run:

pip install --no-deps pipreqs
pip install yarg==0.1.9 docopt==0.6.2
Usage
Usage:
    pipreqs [options] [<path>]

Arguments:
    <path>                The path to the directory containing the application files for which a requirements file
                          should be generated (defaults to the current working directory)

Options:
    --use-local           Use ONLY local package info instead of querying PyPI
    --pypi-server <url>   Use custom PyPi server
    --proxy <url>         Use Proxy, parameter will be passed to requests library. You can also just set the
                          environments parameter in your terminal:
                          $ export HTTP_PROXY="http://10.10.1.10:3128"
                          $ export HTTPS_PROXY="https://10.10.1.10:1080"
    --debug               Print debug information
    --ignore <dirs>...    Ignore extra directories, each separated by a comma
    --no-follow-links     Do not follow symbolic links in the project
    --ignore-errors       Ignore errors while scanning files
    --encoding <charset>  Use encoding parameter for file open
    --savepath <file>     Save the list of requirements in the given file
    --print               Output the list of requirements in the standard output
    --force               Overwrite existing requirements.txt
    --diff <file>         Compare modules in requirements.txt to project imports
    --clean <file>        Clean up requirements.txt by removing modules that are not imported in project
    --mode <scheme>       Enables dynamic versioning with <compat>, <gt> or <non-pin> schemes
                          <compat> | e.g. Flask~=1.1.2
                          <gt>     | e.g. Flask>=1.1.2
                          <no-pin> | e.g. Flask
    --scan-notebooks      Look for imports in jupyter notebook files.
Example
$ pipreqs /home/project/location
Successfully saved requirements file in /home/project/location/requirements.txt
Contents of requirements.txt

wheel==0.23.0
Yarg==0.1.9
docopt==0.6.2