# RecurCIPY

Provides Py interface to Configure CI for GitHub.

## How to begin:

```sh
git checkout -b my_yaml_ci_written_in_python
pip install recurcipy

# Generate you first configuration from template
python -m recurcipy --hello-world

git commit -m "Hello World"
git push --set-upstream origin my_yaml_ci_written_in_python

# Create PR for the pushed branch - Enjoy Your Hello World CI prepared for by recurCIPY
```

## How to continue:

```git
# Play around with generated py configuration in ./ci/configs/* and update yaml files:
python -m recurcipy --generate

# commit, push, repeat
```