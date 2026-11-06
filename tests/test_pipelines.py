"""Guard the .tekton definitions against a failure the first in-cluster run found."""
import re
from pathlib import Path

TEKTON = Path(__file__).resolve().parents[1] / ".tekton"


def test_steps_never_use_a_subdirectory_of_the_shared_volume_as_working_dir():
    """The container runtime creates a missing workingDir as root when the pod starts, before
    any step runs, so a non-root fetch step cannot write into it (Permission denied).
    Thin triggers that only reference a pipeline (release.yaml) have no steps to check."""
    for f in TEKTON.glob("*.yaml"):
        if not re.search(r"^\s*steps:", f.read_text(), re.M):
            continue
        dirs = re.findall(r"^\s*workingDir:\s*(\S+)", f.read_text(), re.M)
        assert dirs, f
        assert set(dirs) == {"/workspace"}, (f.name, dirs)


def test_pipelines_do_not_depend_on_tar_or_gzip():
    """The pinned UBI 10 Python image has neither."""
    for f in TEKTON.glob("*.yaml"):
        for line in f.read_text().splitlines():
            code = line.split("#", 1)[0]
            assert not re.search(r"(^|[|;&(]\s*)\s*(tar|gzip|gunzip)\s", code), (f.name, line)
