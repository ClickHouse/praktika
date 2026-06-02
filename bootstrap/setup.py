from setuptools import find_packages, setup


setup(
    name="praktika-bootstrap",
    version="0.1.0",
    description="Thin bootstrap launcher for versioned Praktika workloads",
    packages=find_packages(where="src", include=["praktika_bootstrap*"]),
    package_dir={"": "src"},
    install_requires=[
        "boto3>=1.18.0",
        "PyJWT>=2.4.0",
        "cryptography>=42.0.0",
        "requests>=2.25.0",
    ],
    entry_points={
        "console_scripts": [
            "praktika_bootstrap=praktika_bootstrap.main:main",
        ],
    },
)
