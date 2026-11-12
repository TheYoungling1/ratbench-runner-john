# Failing-test error types by method

Figure 3 style: share of failing tests by exception type, in environments that built (EBSR success), all 200 repositories (rat50, rat50new, envbench). Failing tests are those that ran and failed or errored, i.e. tests lost from ESSR. Messages come from each method's JUnit report, matched by test ID to the paper rows; repositories whose failures could not be matched are excluded.

## Graph2Env: 36,979 failing tests from 91 repositories

**Repositories counted:** Graph2Env built 149 repositories (EBSR success, failure messages available). 58 of them passed every test and have no failures to classify; the other **91** had at least one failing test. These 91 are the population of this table, and the denominator of "% of repos".

| Error type | Share of failing tests | Failing tests | Repos with this error | % of repos (of those with failing tests) |
|---|---:|---:|---:|---:|
| OperationalError | 45.0% | 16,635 | 12 | 13.2% |
| RuntimeError | 19.0% | 7,016 | 12 | 13.2% |
| RuntimeWarning | 16.5% | 6,096 | 1 | 1.1% |
| APIConnectionError | 3.9% | 1,448 | 1 | 1.1% |
| AssertionError | 2.4% | 898 | 47 | 51.6% |
| ValueError | 1.0% | 380 | 13 | 14.3% |
| Failed | 1.0% | 373 | 13 | 14.3% |
| DockerException | 1.0% | 369 | 2 | 2.2% |
| all other types | 10.2% | 3,764 |  |  |

## Claude Code: 24,031 failing tests from 83 repositories

**Repositories counted:** Claude Code built 133 repositories (EBSR success, failure messages available). 50 of them passed every test and have no failures to classify; the other **83** had at least one failing test. These 83 are the population of this table, and the denominator of "% of repos".

| Error type | Share of failing tests | Failing tests | Repos with this error | % of repos (of those with failing tests) |
|---|---:|---:|---:|---:|
| OperationalError | 63.7% | 15,307 | 11 | 13.3% |
| RuntimeError | 6.5% | 1,574 | 10 | 12.0% |
| APIConnectionError | 6.0% | 1,448 | 1 | 1.2% |
| AssertionError | 4.8% | 1,151 | 43 | 51.8% |
| AttributeError | 2.4% | 587 | 18 | 21.7% |
| ValueError | 1.7% | 417 | 10 | 12.0% |
| APITimeoutError | 1.5% | 362 | 1 | 1.2% |
| DockerException | 1.3% | 322 | 1 | 1.2% |
| all other types | 11.9% | 2,863 |  |  |

## Repo2Run: 15,541 failing tests from 55 repositories

**Repositories counted:** Repo2Run built 83 repositories (EBSR success, failure messages available). 28 of them passed every test and have no failures to classify; the other **55** had at least one failing test. These 55 are the population of this table, and the denominator of "% of repos".

| Error type | Share of failing tests | Failing tests | Repos with this error | % of repos (of those with failing tests) |
|---|---:|---:|---:|---:|
| AssertionError | 58.1% | 9,022 | 27 | 49.1% |
| APIConnectionError | 9.3% | 1,448 | 1 | 1.8% |
| AttributeError | 7.5% | 1,166 | 9 | 16.4% |
| OperationalError | 5.0% | 784 | 3 | 5.5% |
| ModuleNotFoundError | 3.8% | 591 | 6 | 10.9% |
| RuntimeError | 2.8% | 440 | 7 | 12.7% |
| Failed | 2.5% | 386 | 11 | 20.0% |
| DockerException | 2.1% | 322 | 1 | 1.8% |
| all other types | 8.9% | 1,382 |  |  |

## RAT: 4,232 failing tests from 34 repositories

**Repositories counted:** RAT built 83 repositories (EBSR success, failure messages available). 49 of them passed every test and have no failures to classify; the other **34** had at least one failing test. These 34 are the population of this table, and the denominator of "% of repos".

| Error type | Share of failing tests | Failing tests | Repos with this error | % of repos (of those with failing tests) |
|---|---:|---:|---:|---:|
| APIConnectionError | 34.2% | 1,448 | 1 | 2.9% |
| OperationalError | 20.7% | 876 | 2 | 5.9% |
| AssertionError | 9.3% | 395 | 17 | 50.0% |
| APITimeoutError | 8.6% | 362 | 1 | 2.9% |
| DockerException | 7.6% | 322 | 1 | 2.9% |
| ValueError | 4.1% | 174 | 4 | 11.8% |
| HTTPStatusError | 3.0% | 128 | 1 | 2.9% |
| FileNotFoundError | 2.4% | 100 | 6 | 17.6% |
| all other types | 10.1% | 427 |  |  |
| *RuntimeError* | 0.0% | 1 | 1 | 2.9% |
