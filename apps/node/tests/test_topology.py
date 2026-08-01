from subprocess import CompletedProcess

from termflow_node.tmux.topology import TopologyReader


class QueryRunner:
    def run_command(self, *arguments: str):
        if arguments[0] == "list-windows":
            stdout = "$0 main @0 0 1 project\\ a\n"
        else:
            stdout = "$0 @0 %1 0 1 0 80 24 shell\\ title\n"
        return CompletedProcess(list(arguments), 0, stdout=stdout, stderr="")


def test_topology_queries_group_panes_and_only_increment_on_change() -> None:
    reader = TopologyReader(QueryRunner())
    first = reader.read()
    second = reader.read()
    assert first == second
    assert first.revision == 1
    assert first.session_id == "$0"
    assert first.session_name == "main"
    assert first.windows[0].name == "project a"
    assert first.windows[0].panes[0].title == "shell title"


def test_topology_revision_increments_after_value_change() -> None:
    runner = QueryRunner()
    reader = TopologyReader(runner)
    first = reader.read()
    original = runner.run_command

    def changed(*arguments: str):
        result = original(*arguments)
        if arguments[0] == "list-panes":
            result.stdout = result.stdout.replace("80 24", "100 30")
        return result

    runner.run_command = changed
    second = reader.read()
    assert second.revision == first.revision + 1
    assert second.windows[0].panes[0].width == 100
