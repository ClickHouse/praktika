from recurcipy import Shell, YamlGenerator

if __name__ == '__main__':
    Shell.check("git status")
    YamlGenerator().generate(name="Pull Request", on=YamlGenerator.WorkflowTrigers.PULL_REQUEST)
    YamlGenerator().push()
