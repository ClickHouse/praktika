import zipfile

from praktika.infrastructure.lambda_function import Lambda


def test_lambda_packaging_vendors_python_dependencies(monkeypatch, tmp_path):
    source = tmp_path / "handler.py"
    source.write_text("def handler(event, context):\n    return 1\n")

    staged_pkg = []

    def _fake_run(cmd, check):
        assert check is True
        assert "--platform" in cmd
        assert cmd[cmd.index("--platform") + 1] == "manylinux2014_x86_64"
        assert "--python-version" in cmd
        assert cmd[cmd.index("--python-version") + 1] == "3.11"
        assert "--target" in cmd
        target_dir = cmd[cmd.index("--target") + 1]
        pkg_dir = tmp_path / "fake_dep"
        pkg_dir.mkdir(exist_ok=True)
        (pkg_dir / "__init__.py").write_text("VALUE = 1\n")
        import shutil

        shutil.copytree(pkg_dir, __import__("pathlib").Path(target_dir) / "fake_dep")
        staged_pkg.append(target_dir)
        return None

    monkeypatch.setattr("subprocess.run", _fake_run)

    cfg = Lambda.Config(
        name="test-lambda",
        path=str(source),
        handler="handler.handler",
        python_dependencies=["fake-dep==1.0.0"],
    )
    zip_buffer = cfg._package_lambda_code(
        cfg.path, cfg.include_files, cfg.python_dependencies
    )

    assert staged_pkg
    with zipfile.ZipFile(zip_buffer) as zf:
        names = set(zf.namelist())
    assert "handler.py" in names
    assert "fake_dep/__init__.py" in names
