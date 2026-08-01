from termflow_node.tmux.client_size import ClientSizeResolver, TerminalSize
from termflow_node.tmux.runner import TmuxClient


class ClientRunner:
    def __init__(self, clients: list[TmuxClient]) -> None:
        self.clients = clients

    def list_clients(self, session_id: str) -> list[TmuxClient]:
        assert session_id == "$0"
        return self.clients


def _client(
    tty: str,
    activity: int,
    cols: int,
    rows: int,
    *,
    control: bool = False,
) -> TmuxClient:
    return TmuxClient(
        tty=tty,
        activity=activity,
        cols=cols,
        rows=rows,
        control_mode=control,
        termname="xterm-256color",
    )


def test_recent_local_client_wins_and_proxy_and_control_clients_are_excluded() -> None:
    runner = ClientRunner(
        [
            _client("/dev/pts/local-old", 10, 100, 30),
            _client("/dev/pts/proxy", 30, 200, 60),
            _client("/dev/pts/control", 40, 300, 70, control=True),
            _client("/dev/pts/local-new", 20, 140, 50),
        ]
    )
    resolver = ClientSizeResolver(runner, "$0", creation_size=TerminalSize(24, 80))
    assert resolver.resolve(proxy_ttys={"/dev/pts/proxy"}) == TerminalSize(50, 140)

    runner.clients = []
    assert resolver.resolve(proxy_ttys=set()) == TerminalSize(50, 140)


def test_size_falls_back_to_creation_size_then_80_by_24() -> None:
    runner = ClientRunner([])
    assert ClientSizeResolver(
        runner,
        "$0",
        creation_size=TerminalSize(32, 90),
    ).resolve(proxy_ttys=set()) == TerminalSize(32, 90)
    assert ClientSizeResolver(runner, "$0").resolve(proxy_ttys=set()) == TerminalSize(24, 80)


def test_size_ignores_non_control_clients_until_they_report_a_positive_grid() -> None:
    runner = ClientRunner([_client("/dev/pts/pending", 10, 0, 0)])
    resolver = ClientSizeResolver(runner, "$0", creation_size=TerminalSize(24, 80))

    assert resolver.resolve(proxy_ttys=set()) == TerminalSize(24, 80)
