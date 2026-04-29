from pathlib import Path
from unittest.mock import MagicMock

from skaal.deploy.local_automation import deploy_local_stack


def test_deploy_local_stack_uses_tagged_image_ref(monkeypatch):
    stack_ref = MagicMock()
    stack_ref.outputs.return_value = {"appUrl": MagicMock(value="http://localhost:8000")}

    monkeypatch.setattr(
        "skaal.deploy.local_automation._build_local_image",
        lambda artifacts_dir, image_name: "sha256:local-image",
    )
    monkeypatch.setattr(
        "skaal.deploy.local_automation._create_or_select_stack",
        lambda artifacts_dir, stack: (stack_ref, {"name": "skaal-test"}),
    )

    app_url = deploy_local_stack(
        Path("artifacts"),
        stack="local",
        yes=True,
        app_name="Test App",
    )

    assert app_url == "http://localhost:8000"
    config_value = stack_ref.set_config.call_args_list[0].args[1]
    assert stack_ref.set_config.call_args_list[0].args[0] == "localImageRef"
    # The image ID (sha256:…) is preferred over the mutable tag to avoid
    # Docker Desktop i/o timeout when Pulumi lists images.
    assert config_value.value == "sha256:local-image"
