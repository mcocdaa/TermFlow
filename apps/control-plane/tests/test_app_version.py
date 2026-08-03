from termflow_control_plane import __version__
from termflow_control_plane.app import create_app
from termflow_control_plane.config import Settings
from termflow_control_plane.persistence.database import Database


def test_app_reports_materialized_package_version(settings: Settings) -> None:
    app = create_app(settings=settings, database=Database(settings.database_url))

    assert app.version == __version__
