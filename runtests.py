#!/usr/bin/python3
import argparse
import collections
import decimal
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap

from typing import DefaultDict, List

import container
import problems


def _main() -> None:
    rootDirectory = problems.repositoryRoot()

    parser = argparse.ArgumentParser('Run tests')
    parser.add_argument('--ci',
                        action='store_true',
                        help='Signal that this is being run from the CI.')
    parser.add_argument(
        '--all',
        action='store_true',
        help='Consider all problems, instead of only those that have changed')
    parser.add_argument('--verbose',
                        action='store_true',
                        help='Verbose logging')
    parser.add_argument('--results-directory',
                        default=os.path.join(rootDirectory, 'results'),
                        help='Directory to store the results of the runs')
    parser.add_argument(
        '--only-pull-image',
        action='store_true',
        help='Don\'t run tests: only download the Docker container')
    parser.add_argument('problem_paths',
                        metavar='PROBLEM',
                        type=str,
                        nargs='*')
    args = parser.parse_args()

    logging.basicConfig(format='%(asctime)s: %(message)s',
                        level=logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger('urllib3').setLevel(logging.CRITICAL)

    if args.only_pull_image:
        container.getImageName(args.ci)
        sys.exit(0)

    anyFailure = False

    if os.path.isdir(args.results_directory):
        shutil.rmtree(args.results_directory)
    os.makedirs(args.results_directory)

    for p in problems.problems(allProblems=args.all,
                               rootDirectory=rootDirectory,
                               problemPaths=args.problem_paths):
        logging.info('Testing problem: %s...', p.title)

        problemResultsDirectory = os.path.join(args.results_directory, p.path)
        os.makedirs(problemResultsDirectory)
        # The results are written with the container's UID, which does not
        # necessarily match the caller's UID. To avoid that problem, we create
        # the results directory with very lax permissions so that the container
        # can write it.
        os.chmod(problemResultsDirectory, 0o777)
        with open(os.path.join(problemResultsDirectory, 'ci.log'), 'w') as f:
            pass
        # Also make the ci log's permissions very lax.
        os.chmod(os.path.join(problemResultsDirectory, 'ci.log'), 0o666)

        processResult = subprocess.run([
            'docker',
            'run',
            '--rm',
            '--volume',
            f'{rootDirectory}:/src',
            container.getImageName(args.ci),
            '-oneshot=ci',
            '-input',
            p.path,
            '-results',
            os.path.relpath(problemResultsDirectory, rootDirectory),
        ],
                                       universal_newlines=True,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE,
                                       cwd=rootDirectory)

        if processResult.returncode != 0:
            problems.error(f'Failed to run {p.title}:\n{processResult.stderr}',
                           filename=os.path.join(p.path, 'settings.json'))
            anyFailure = True
            continue

        # The CI might have written a log, but the stderr contents have a few
        # more things in it.
        with open(os.path.join(problemResultsDirectory, 'ci.log'), 'w') as f:
            f.write(processResult.stderr)

        report = json.loads(processResult.stdout)

        if report['state'] != 'passed':
            anyFailure = True

        if report['state'] == 'skipped':
            problems.error(
                f'Skipped {p.title}:\n'
                'tests/tests.json, settings.json, outs, or testplan are '
                'probably missing or invalid.',
                filename=os.path.join(p.path, 'settings.json'))
            continue

        for testResult in report.get('tests', []):
            testedFile = os.path.join(p.path, 'tests', testResult['filename'])

            if testResult['type'] == 'solutions':
                expected = dict(testResult['solution'])
                del (expected['filename'])
                if not expected:
                    # If there are no constraints, by default expect the run to
                    # be accepted.
                    expected['verdict'] = 'AC'
                logsDirectory = os.path.join(problemResultsDirectory,
                                             str(testResult['index']))
            else:
                expected = {'verdict': 'AC'}
                logsDirectory = os.path.join(problemResultsDirectory,
                                             str(testResult['index']),
                                             'validator')

            got = {
                'verdict': testResult.get('result', {}).get('verdict'),
                'score': testResult.get('result', {}).get('score'),
            }

            logging.info(
                f'    {testResult["type"]:10} | '
                f'{testResult["filename"][:40]:40} | '
                f'{testResult["state"]:8} | '
                f'expected={expected} got={got} | '
                f'logs at {os.path.relpath(logsDirectory, rootDirectory)}')

            failureMessages: DefaultDict[
                str, List[str]] = collections.defaultdict(list)

            normalizedScore = decimal.Decimal(got.get('score', 0))
            scaledScore = round(normalizedScore, 15) * 100

            if scaledScore != int(scaledScore):
                anyFailure = True

                failureMessage = (f'Score isn\'t an integer! '
                                  f'Got: {scaledScore}\n')
                logging.error(failureMessage)
                failureMessages[testedFile].append(failureMessage)

            if testResult['state'] != 'passed':
                failedCases = {
                    c['name']
                    for g in testResult['result']['groups'] for c in g['cases']
                    if c['verdict'] != 'AC'
                }

                if os.path.isdir(logsDirectory):
                    for stderrFilename in sorted(os.listdir(logsDirectory)):
                        caseName = os.path.splitext(stderrFilename)[0]

                        if not stderrFilename.endswith('.err'):
                            continue
                        if caseName not in failedCases:
                            continue

                        if testResult['type'] == 'solutions':
                            associatedFile = testedFile
                        else:
                            associatedFile = os.path.join(
                                p.path, 'cases', f'{caseName}.in')

                        with open(os.path.join(logsDirectory, stderrFilename),
                                  'r') as out:
                            failureMessage = (
                                f"{stderrFilename}:"
                                f"\n{textwrap.indent(out.read(), '    ')}")
                            logging.info(failureMessage)
                            failureMessages[associatedFile].append(
                                failureMessage)
                else:
                    logging.warning('Logs directory %r not found.',
                                    logsDirectory)

            if args.ci:
                for (path, messages) in failureMessages.items():
                    problems.ci_error('\n'.join(messages), filename=path)

        logging.info(f'Results for {p.title}: {report["state"]}')
        logging.info(f'    Full logs and report in {problemResultsDirectory}')

    if anyFailure:
        sys.exit(1)


if __name__ == '__main__':
    _main()
