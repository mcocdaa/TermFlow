from subprocess import CompletedProcess

from termflow_node.tmux.topology import TopologyReader


class QueryRunner:
    def run_command(self, *arguments: str):
        if arguments[0] == "list-windows":
            stdout = "$0 main @0 0 1 project\\ a\n"
        else:
            stdout = "$0 @0 %1 0 1 0 80 24 4 7 python shell\\ title\n"
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
    assert first.windows[0].panes[0].left == 4
    assert first.windows[0].panes[0].top == 7
    assert first.windows[0].panes[0].current_command == "python"


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


def test_topology_reader_scopes_queries_to_stable_session_id() -> None:
    runner = QueryRunner()
    TopologyReader(runner, session_id="$9").read()
    # The fake runner is intentionally permissive; production calls carry a discrete argv target.
    # This assertion is made through a recording wrapper to ensure no name interpolation occurs.
    calls: list[tuple[str, ...]] = []

    class RecordingRunner(QueryRunner):
        def run_command(self, *arguments: str):
            calls.append(arguments)
            return super().run_command(*arguments)

    TopologyReader(RecordingRunner(), session_id="$9").read()
    assert calls[0][:3] == ("list-windows", "-t", "$9")
    assert calls[1][:4] == ("list-panes", "-s", "-t", "$9")
