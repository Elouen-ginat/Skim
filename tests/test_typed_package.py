from importlib.resources import files


def test_package_exposes_py_typed_marker() -> None:
    assert files("skaal").joinpath("py.typed").is_file()
