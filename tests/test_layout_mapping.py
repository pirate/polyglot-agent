import unittest
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from polyglot_map.cli import Target, map_target_path, parse_languages, resolve_target_path, snake_case


class LayoutMappingTests(unittest.TestCase):
    def test_snake_case_preserves_initialisms(self) -> None:
        self.assertEqual(snake_case("JSONLEventBridge"), "jsonl_event_bridge")
        self.assertEqual(snake_case("SQLiteEventBridge"), "sqlite_event_bridge")
        self.assertEqual(snake_case("HTTPEventBridge"), "http_event_bridge")
        self.assertEqual(snake_case("BaseEvent_EventBus_proxy"), "base_event_event_bus_proxy")

    def test_parse_languages_normalizes_aliases_and_dedupes(self) -> None:
        self.assertEqual(parse_languages(["ts,python,golang", "go"]), ("typescript", "python", "go"))

    def test_default_implementation_mapping(self) -> None:
        rel = PurePosixPath("src/EventBus.ts")

        self.assertEqual(
            map_target_path(rel, Target("python", Path("/repo"), package_name="abxbus")),
            Path("/repo/abxbus/event_bus.py"),
        )
        self.assertEqual(map_target_path(rel, Target("go", Path("/repo/go"))), Path("/repo/go/event_bus.go"))
        self.assertEqual(map_target_path(rel, Target("rust", Path("/repo/rust"))), Path("/repo/rust/src/event_bus.rs"))
        self.assertEqual(
            map_target_path(rel, Target("ruby", Path("/repo/ruby"), package_name="abxbus")),
            Path("/repo/ruby/lib/abxbus/event_bus.rb"),
        )

    def test_root_test_mapping(self) -> None:
        rel = PurePosixPath("tests/EventBus.test.ts")

        self.assertEqual(map_target_path(rel, Target("python", Path("/repo"))), Path("/repo/tests/test_event_bus.py"))
        self.assertEqual(map_target_path(rel, Target("go", Path("/repo/go"))), Path("/repo/go/tests/event_bus_test.go"))
        self.assertEqual(
            map_target_path(rel, Target("rust", Path("/repo/rust"))),
            Path("/repo/rust/tests/test_event_bus.rs"),
        )
        self.assertEqual(
            map_target_path(rel, Target("ruby", Path("/repo/ruby"))),
            Path("/repo/ruby/test/test_event_bus.rb"),
        )

    def test_embedded_test_dot_mapping_keeps_requested_go_shape(self) -> None:
        rel = PurePosixPath("src/somemodule/tests/test.SomeClass.ts")

        self.assertEqual(
            map_target_path(rel, Target("python", Path("/repo"))),
            Path("/repo/tests/somemodule/test_some_class.py"),
        )
        self.assertEqual(
            map_target_path(rel, Target("go", Path("/repo/go"))),
            Path("/repo/go/somemodule/SomeClass_test.go"),
        )
        self.assertEqual(
            map_target_path(rel, Target("rust", Path("/repo/rust"))),
            Path("/repo/rust/tests/somemodule/test_some_class.rs"),
        )
        self.assertEqual(
            map_target_path(rel, Target("ruby", Path("/repo/ruby"))),
            Path("/repo/ruby/test/somemodule/test_some_class.rb"),
        )

    def test_existing_pascal_case_python_source_stem_is_authoritative(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing = root / "example_pkg" / "SourceClient.py"
            existing.parent.mkdir()
            existing.write_text("class SourceClient: ...\n")

            path, source, default_path = resolve_target_path(
                PurePosixPath("SourceClient.ts"),
                Target("python", root, package_name="example_pkg"),
            )

            self.assertEqual(path, existing)
            self.assertEqual(source, "existing-equivalent")
            self.assertEqual(default_path, root / "example_pkg" / "source_client.py")

    def test_existing_go_command_entrypoint_is_authoritative(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            existing = root / "command" / "main.go"
            existing.parent.mkdir()
            existing.write_text("package main\n")

            path, source, default_path = resolve_target_path(PurePosixPath("command.ts"), Target("go", root))

            self.assertEqual(path, existing)
            self.assertEqual(source, "existing-equivalent")
            self.assertEqual(default_path, root / "command.go")


if __name__ == "__main__":
    unittest.main()
