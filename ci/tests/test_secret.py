from praktika.secret import Secret


class _SSMClient:
    def __init__(self):
        self.calls = []

    def get_parameter(self, **kwargs):
        self.calls.append(("get_parameter", kwargs))
        return {"Parameter": {"Name": kwargs["Name"], "Value": "single-value"}}

    def get_parameters(self, **kwargs):
        self.calls.append(("get_parameters", kwargs))
        return {
            "Parameters": [
                {"Name": "second", "Value": "value-2"},
                {"Name": "first", "Value": "value-1"},
            ]
        }


class _SecretsManagerClient:
    def __init__(self):
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(("get_secret_value", kwargs))
        if kwargs["SecretId"] == "plain":
            return {"SecretString": "plain-value"}
        return {"SecretString": '{"token": "abc", "password": "def"}'}


def test_aws_ssm_parameter_lookup_uses_boto3(monkeypatch):
    clients = {}

    def fake_client(service_name, region_name=None):
        client = _SSMClient()
        clients[service_name] = (region_name, client)
        return client

    monkeypatch.setattr("boto3.client", fake_client)

    value = Secret.Config(
        name="cidb-connection",
        type=Secret.Type.AWS_SSM_PARAMETER,
        region="eu-north-1",
    ).get_value()

    region, client = clients["ssm"]
    assert value == "single-value"
    assert region == "eu-north-1"
    assert client.calls == [
        (
            "get_parameter",
            {"Name": "cidb-connection", "WithDecryption": True},
        )
    ]


def test_aws_ssm_parameter_lookup_defaults_to_settings_region(monkeypatch):
    clients = {}

    def fake_client(service_name, region_name=None):
        client = _SSMClient()
        clients[service_name] = (region_name, client)
        return client

    monkeypatch.setattr("boto3.client", fake_client)
    monkeypatch.setattr("praktika.settings.Settings.AWS_REGION", "eu-west-3", raising=False)

    value = Secret.Config(
        name="cidb-connection",
        type=Secret.Type.AWS_SSM_PARAMETER,
    ).get_value()

    region, client = clients["ssm"]
    assert value == "single-value"
    assert region == "eu-west-3"
    assert client.calls == [
        (
            "get_parameter",
            {"Name": "cidb-connection", "WithDecryption": True},
        )
    ]


def test_aws_ssm_parameter_lookup_defaults_to_aws_env_region(monkeypatch):
    clients = {}

    def fake_client(service_name, region_name=None):
        client = _SSMClient()
        clients[service_name] = (region_name, client)
        return client

    monkeypatch.setattr("boto3.client", fake_client)
    monkeypatch.setattr("praktika.settings.Settings.AWS_REGION", "", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")

    value = Secret.Config(
        name="cidb-connection",
        type=Secret.Type.AWS_SSM_PARAMETER,
    ).get_value()

    region, client = clients["ssm"]
    assert value == "single-value"
    assert region == "ap-south-1"
    assert client.calls == [
        (
            "get_parameter",
            {"Name": "cidb-connection", "WithDecryption": True},
        )
    ]


def test_aws_ssm_parameters_preserve_requested_order(monkeypatch):
    client = _SSMClient()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: client)

    value = Secret.Config(
        name=["first", "second"],
        type=Secret.Type.AWS_SSM_PARAMETER,
    ).get_value()

    assert value == ["value-1", "value-2"]
    assert client.calls == [
        (
            "get_parameters",
            {"Names": ["first", "second"], "WithDecryption": True},
        )
    ]


def test_aws_secret_lookup_uses_boto3(monkeypatch):
    client = _SecretsManagerClient()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: client)

    value = Secret.Config(
        name="plain",
        type=Secret.Type.AWS_SSM_SECRET,
    ).get_value()

    assert value == "plain-value"
    assert client.calls == [("get_secret_value", {"SecretId": "plain"})]


def test_aws_secret_key_batch_lookup_uses_single_call_per_root(monkeypatch):
    client = _SecretsManagerClient()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: client)

    value = Secret.Config(
        name=["json.token", "json.password"],
        type=Secret.Type.AWS_SSM_SECRET,
    ).get_value()

    assert value == ["abc", "def"]
    assert client.calls == [("get_secret_value", {"SecretId": "json"})]
