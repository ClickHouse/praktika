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


def test_lambda_deploy_readds_api_gateway_permission_when_api_already_exists(
    monkeypatch, tmp_path
):
    source = tmp_path / "handler.py"
    source.write_text("def handler(event, context):\n    return 1\n")

    cfg = Lambda.Config(
        name="webhook",
        path=str(source),
        handler="handler.handler",
        role_name="lambda-role",
        api_gateway=True,
        region="eu-north-1",
    )
    cfg.ext.update(
        {
            "role_arn": "arn:aws:iam::123456789012:role/lambda-role",
            "runtime": "python3.11",
            "environment": {},
            "handler": "handler.handler",
            "timeout": 3,
            "memory_size": 128,
        }
    )

    monkeypatch.setattr(cfg, "_validate_secrets", lambda: None)
    monkeypatch.setattr(cfg, "fetch", lambda: cfg)
    monkeypatch.setattr(
        cfg,
        "_package_lambda_code",
        lambda *args, **kwargs: __import__("io").BytesIO(b"zip"),
    )
    monkeypatch.setattr(cfg, "_dump_api_endpoint", lambda *args, **kwargs: None)

    calls = {"add_permission": None}

    class _LambdaClient:
        class exceptions:
            class ResourceNotFoundException(Exception):
                pass

            class ResourceConflictException(Exception):
                pass

        def get_function(self, FunctionName):
            return {
                "Configuration": {
                    "CodeSha256": __import__("base64").b64encode(
                        __import__("hashlib").sha256(b"zip").digest()
                    ).decode("utf-8"),
                    "FunctionArn": "arn:aws:lambda:eu-north-1:123456789012:function:webhook",
                }
            }

        def add_permission(self, **kwargs):
            calls["add_permission"] = kwargs

    class _ApiGwClient:
        def get_apis(self):
            return {
                "Items": [
                    {
                        "Name": "webhook-API",
                        "ApiId": "api123",
                        "ApiEndpoint": "https://example.execute-api.eu-north-1.amazonaws.com",
                    }
                ]
            }

    monkeypatch.setattr(
        "praktika.infrastructure.lambda_function.aws_client",
        lambda service, region, name: _LambdaClient()
        if service == "lambda"
        else _ApiGwClient(),
    )

    cfg.deploy()

    assert calls["add_permission"] == {
        "FunctionName": "webhook",
        "StatementId": "AllowAPIGatewayInvoke",
        "Action": "lambda:InvokeFunction",
        "Principal": "apigateway.amazonaws.com",
        "SourceArn": "arn:aws:execute-api:eu-north-1:123456789012:api123/*/*",
    }
