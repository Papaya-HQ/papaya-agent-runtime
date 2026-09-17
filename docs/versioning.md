# Versioning and releases

## The tag is the version

This runtime is always run from a checkout. There is no wheel, no `pip install`,
nothing that could stamp a version into the package as it was built — so a literal
version in a file would have to be bumped by hand, and would name the same build on
every machine whenever nobody did. That is exactly what happened: `__version__` and
`pyproject.toml` both said `0.0.0` for the whole of the project's life, and Papaya
Desktop's machine card dutifully printed "Managed by Papaya Agent Runtime 0.0.0" for
every machine anybody ever connected.

So the version lives in git, as an annotated tag `v<major>.<minor>.<patch>`, and
[`src/papaya_agent_runtime/version.py`](../src/papaya_agent_runtime/version.py) reads
it back out of the checkout. `pyproject.toml` declares `dynamic = ["version"]` and
states no number of its own.

Everything that reports a version reports that one:

    ppy version
    ppy capabilities --json      # the `version` key, which the desktop prints verbatim
    papaya_agent_runtime.__version__

What you get depends on where the checkout stands relative to its newest release tag:

| The checkout | The version |
|---|---|
| HEAD *is* `v0.1.3`, nothing modified | `0.1.3` |
| two commits past `v0.1.3` | `0.1.3.post2+gabc1234` |
| on `v0.1.3` with a tracked file edited | `0.1.3+gabc1234.dirty` |
| never tagged | `0.0.0+gabc1234` |
| no git, not a checkout, or no commits | `0.0.0` |

All of these are valid [PEP 440](https://peps.python.org/pep-0440/) versions, so
anything reading one can parse it. Only an exact tag on a clean tree gets to be a
bare `X.Y.Z`: `0.1.3+dirty` would claim to *be* release 0.1.3, which is the one thing
a modified checkout is not, so the commit and the dirt go in the local segment
instead. "Modified" is git's own meaning — a tracked file differs from HEAD — so a
scratch directory beside the source does not change which build you are running.

Reading the version starts a `git describe`, and starting anything can fail. It never
raises and it never blocks for long: `git describe --dirty` walks the working tree, so
when it exceeds its two seconds the answer still names the commit through a `git
rev-parse` that reads one ref. A flat `0.0.0` means there was genuinely nothing to
name. The whole of it stays far inside the ten seconds the Papaya client allows its
connect-time probe.

## Every merge to main is a release

[`.github/workflows/release.yml`](../.github/workflows/release.yml) runs on every push
to `main`. It finds the newest `v<major>.<minor>.<patch>` tag, bumps the patch by one,
creates that annotated tag on the pushed commit, and publishes a GitHub release with
auto-generated notes. A repository that has never been released starts at `v0.1.0`.

Nobody has to remember to bump anything, and nothing has to be merged to release —
which matters here, because the `main` ruleset requires a pull request and a green
`ci-ok` with no bypass actors, so no workflow could commit a version bump even if we
wanted one. A tag, a workflow may push.

It is idempotent. The computation lives in the package, not in the workflow's shell:

    python3 -m papaya_agent_runtime.version --next

prints the tag to create, or an empty line when HEAD already carries a release tag —
so a re-run, or a commit somebody tagged by hand, tags nothing and exits green. The
sort is in Python for a reason: `git tag` orders lexically, which puts `v0.1.10`
before `v0.1.9`, and a release process that got that wrong would go backwards. That
one sort is unit-tested in [`tests/test_version.py`](../tests/test_version.py).

If the tag is pushed but publishing the release then fails, the job goes red and the
tag stays. Re-running will not create a second tag for one build; publish the missing
release by hand instead:

    gh release create v0.1.5 --generate-notes

## Minors and majors are cut by hand

The workflow only ever adds a patch. When a release deserves a minor or a major, tag
the commit on `main` yourself and push it:

    git tag -a v0.2.0 -m "papaya-agent-runtime v0.2.0"
    git push origin v0.2.0

The next merge to `main` then continues from there, as `v0.2.1`. Nothing else needs
changing: the tag is the version, so cutting one by hand is the whole operation.
