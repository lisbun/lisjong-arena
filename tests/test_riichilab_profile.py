"""Arena-owned RiichiLab bot実行profile (Issue #19) のunit test。

profile -> credential環境変数 -> Policy -> runtime namespaceの一方向mapping、
profile未指定・未知profile・credential未設定のfail closed、他profile
credentialへのfallbackがないこと、secret-freeなruntime summary/pathを、
実WebSocket/RiichiLab接続なしに確認する。

profile identity / credential env var / Policy mappingはmigration元
(`lisjong.riichilab_client.profile`, Issue #44) のcontractをbehavior-preserving
に維持する。
"""

import concurrent.futures
import unittest
from pathlib import Path

from lisjong.policies import MinimalPolicy, TwoStepUkeirePolicy

from lisjong_arena.riichilab.profile import (
    PROFILE_NAMES,
    MissingCredentialError,
    RuntimeProfile,
    UnknownProfileError,
    build_runtime_summary,
    default_trace_path,
    format_runtime_summary,
    resolve_credential,
    resolve_profile,
    runtime_root,
)

_DUMMY_SECRET = "dummy-secret-do-not-leak"


class ProfileMappingTest(unittest.TestCase):
    """3 profileのmappingを固定するregression test。"""

    def test_exactly_three_known_profiles(self) -> None:
        self.assertEqual(
            set(PROFILE_NAMES), {"lisjong-dev", "lisjong-baseline", "lisjong"}
        )

    def test_lisjong_dev_mapping(self) -> None:
        profile = resolve_profile("lisjong-dev")
        self.assertEqual(profile.name, "lisjong-dev")
        self.assertEqual(profile.credential_env_var, "LISJONG_DEV_BOT_TOKEN")
        self.assertEqual(profile.runtime_namespace, "lisjong-dev")
        self.assertIsInstance(profile.policy_factory(), TwoStepUkeirePolicy)

    def test_lisjong_baseline_mapping(self) -> None:
        profile = resolve_profile("lisjong-baseline")
        self.assertEqual(profile.name, "lisjong-baseline")
        self.assertEqual(profile.credential_env_var, "LISJONG_BASELINE_BOT_TOKEN")
        self.assertEqual(profile.runtime_namespace, "lisjong-baseline")
        self.assertIsInstance(profile.policy_factory(), MinimalPolicy)

    def test_lisjong_production_mapping(self) -> None:
        profile = resolve_profile("lisjong")
        self.assertEqual(profile.name, "lisjong")
        self.assertEqual(profile.credential_env_var, "LISJONG_BOT_TOKEN")
        self.assertEqual(profile.runtime_namespace, "lisjong")
        self.assertIsInstance(profile.policy_factory(), MinimalPolicy)

    def test_policy_factory_returns_a_fresh_instance_each_call(self) -> None:
        profile = resolve_profile("lisjong-dev")
        self.assertIsNot(profile.policy_factory(), profile.policy_factory())

    def test_canonical_symbols_are_arena_local(self) -> None:
        self.assertEqual(RuntimeProfile.__module__, "lisjong_arena.riichilab.profile")
        self.assertEqual(resolve_profile.__module__, "lisjong_arena.riichilab.profile")


class ResolveProfileFailClosedTest(unittest.TestCase):
    def test_missing_profile_name_fails_closed(self) -> None:
        with self.assertRaises(UnknownProfileError):
            resolve_profile(None)

    def test_empty_profile_name_fails_closed(self) -> None:
        with self.assertRaises(UnknownProfileError):
            resolve_profile("")

    def test_unknown_profile_name_fails_closed(self) -> None:
        with self.assertRaises(UnknownProfileError):
            resolve_profile("lisjong-production")

    def test_unknown_profile_error_lists_known_profiles_without_guessing(self) -> None:
        with self.assertRaises(UnknownProfileError) as caught:
            resolve_profile("typo-profile")
        message = str(caught.exception)
        for name in PROFILE_NAMES:
            self.assertIn(name, message)


class ResolveCredentialTest(unittest.TestCase):
    def test_reads_token_from_its_own_env_var_only(self) -> None:
        profile = resolve_profile("lisjong-dev")
        token = resolve_credential(
            profile, env={"LISJONG_DEV_BOT_TOKEN": _DUMMY_SECRET}
        )
        self.assertEqual(token, _DUMMY_SECRET)

    def test_missing_credential_fails_closed(self) -> None:
        profile = resolve_profile("lisjong-dev")
        with self.assertRaises(MissingCredentialError):
            resolve_credential(profile, env={})

    def test_empty_credential_fails_closed(self) -> None:
        profile = resolve_profile("lisjong-dev")
        with self.assertRaises(MissingCredentialError):
            resolve_credential(profile, env={"LISJONG_DEV_BOT_TOKEN": ""})

    def test_missing_credential_error_names_the_env_var_without_a_value(self) -> None:
        profile = resolve_profile("lisjong-baseline")
        with self.assertRaises(MissingCredentialError) as caught:
            resolve_credential(profile, env={})
        message = str(caught.exception)
        self.assertIn("LISJONG_BASELINE_BOT_TOKEN", message)

    def test_dev_profile_does_not_fall_back_to_other_profile_credentials(self) -> None:
        dev_profile = resolve_profile("lisjong-dev")
        env = {
            "LISJONG_BASELINE_BOT_TOKEN": "baseline-secret",
            "LISJONG_BOT_TOKEN": "production-secret",
        }
        with self.assertRaises(MissingCredentialError):
            resolve_credential(dev_profile, env=env)

    def test_production_profile_does_not_fall_back_to_dev_or_baseline(self) -> None:
        production_profile = resolve_profile("lisjong")
        env = {
            "LISJONG_DEV_BOT_TOKEN": "dev-secret",
            "LISJONG_BASELINE_BOT_TOKEN": "baseline-secret",
        }
        with self.assertRaises(MissingCredentialError):
            resolve_credential(production_profile, env=env)

    def test_baseline_profile_does_not_fall_back_to_dev_or_production(self) -> None:
        baseline_profile = resolve_profile("lisjong-baseline")
        env = {
            "LISJONG_DEV_BOT_TOKEN": "dev-secret",
            "LISJONG_BOT_TOKEN": "production-secret",
        }
        with self.assertRaises(MissingCredentialError):
            resolve_credential(baseline_profile, env=env)

    def test_each_profile_reads_only_its_own_token_when_all_are_set(self) -> None:
        env = {
            "LISJONG_DEV_BOT_TOKEN": "dev-secret",
            "LISJONG_BASELINE_BOT_TOKEN": "baseline-secret",
            "LISJONG_BOT_TOKEN": "production-secret",
        }
        self.assertEqual(
            resolve_credential(resolve_profile("lisjong-dev"), env=env), "dev-secret"
        )
        self.assertEqual(
            resolve_credential(resolve_profile("lisjong-baseline"), env=env),
            "baseline-secret",
        )
        self.assertEqual(
            resolve_credential(resolve_profile("lisjong"), env=env),
            "production-secret",
        )

    def test_error_message_never_contains_the_credential_value(self) -> None:
        profile = resolve_profile("lisjong-dev")
        with self.assertRaises(MissingCredentialError) as caught:
            resolve_credential(profile, env={"LISJONG_DEV_BOT_TOKEN": ""})
        self.assertNotIn(_DUMMY_SECRET, str(caught.exception))


class RuntimeRootTest(unittest.TestCase):
    """OSユーザーローカル領域の解決を、repository配下を使わない設計として確認する。"""

    def test_windows_uses_localappdata_when_set(self) -> None:
        root = runtime_root(
            platform="win32", env={"LOCALAPPDATA": r"C:\Users\test\AppData\Local"}
        )
        self.assertEqual(root, Path(r"C:\Users\test\AppData\Local") / "lisjong")

    def test_windows_falls_back_to_home_when_localappdata_unset(self) -> None:
        root = runtime_root(platform="win32", env={})
        self.assertEqual(root, Path.home() / "AppData" / "Local" / "lisjong")

    def test_macos_uses_application_support(self) -> None:
        root = runtime_root(platform="darwin", env={})
        self.assertEqual(
            root, Path.home() / "Library" / "Application Support" / "lisjong"
        )

    def test_linux_uses_xdg_data_home_when_set(self) -> None:
        root = runtime_root(platform="linux", env={"XDG_DATA_HOME": "/tmp/xdgdata"})
        self.assertEqual(root, Path("/tmp/xdgdata") / "lisjong")

    def test_linux_falls_back_to_local_share_when_xdg_unset(self) -> None:
        root = runtime_root(platform="linux", env={})
        self.assertEqual(root, Path.home() / ".local" / "share" / "lisjong")

    def test_runtime_root_never_points_inside_the_repository(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        for platform in ("win32", "darwin", "linux"):
            with self.subTest(platform=platform):
                root = runtime_root(platform=platform, env={})
                self.assertNotIn(repo_root, root.parents)
                self.assertNotEqual(root, repo_root)


class DefaultTracePathTest(unittest.TestCase):
    def test_path_is_namespaced_under_profile_runtime_namespace(self) -> None:
        dev_profile = resolve_profile("lisjong-dev")
        path = default_trace_path(dev_profile, platform="linux", env={})
        self.assertEqual(path.parent.name, "lisjong-dev")
        self.assertEqual(path.parent.parent.name, "traces")
        self.assertEqual(path.suffix, ".jsonl")

    def test_different_profiles_use_non_colliding_namespaces(self) -> None:
        dev_path = default_trace_path(
            resolve_profile("lisjong-dev"), platform="linux", env={}
        )
        baseline_path = default_trace_path(
            resolve_profile("lisjong-baseline"), platform="linux", env={}
        )
        self.assertNotEqual(dev_path.parent, baseline_path.parent)

    def test_repeated_calls_for_the_same_profile_do_not_collide(self) -> None:
        profile = resolve_profile("lisjong-dev")
        paths = {
            default_trace_path(profile, platform="linux", env={}) for _ in range(200)
        }
        self.assertEqual(len(paths), 200)

    def test_concurrent_calls_do_not_collide(self) -> None:
        profile = resolve_profile("lisjong-baseline")

        def _make_path() -> str:
            return str(default_trace_path(profile, platform="linux", env={}))

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            paths = list(executor.map(lambda _: _make_path(), range(200)))

        self.assertEqual(len(paths), len(set(paths)))

    def test_path_does_not_embed_a_credential_value(self) -> None:
        profile = resolve_profile("lisjong")
        path = default_trace_path(
            profile, platform="linux", env={"LISJONG_BOT_TOKEN": _DUMMY_SECRET}
        )
        self.assertNotIn(_DUMMY_SECRET, str(path))


class RuntimeSummaryTest(unittest.TestCase):
    def test_summary_lists_profile_policy_mode_and_trace_off(self) -> None:
        profile = resolve_profile("lisjong-baseline")
        summary = build_runtime_summary(
            profile, mode="ranked", trace_path=None, policy=profile.policy_factory()
        )
        text = format_runtime_summary(summary)
        self.assertIn("profile: lisjong-baseline", text)
        self.assertIn("policy: MinimalPolicy", text)
        self.assertIn("mode: ranked", text)
        self.assertIn("trace: off", text)
        self.assertNotIn("trace path:", text)

    def test_summary_shows_trace_path_when_enabled(self) -> None:
        profile = resolve_profile("lisjong-dev")
        summary = build_runtime_summary(
            profile,
            mode="validation",
            trace_path="traces/example.jsonl",
            policy=profile.policy_factory(),
        )
        text = format_runtime_summary(summary)
        self.assertIn("trace: on", text)
        self.assertIn("trace path: traces/example.jsonl", text)

    def test_summary_never_contains_credential_env_var_or_value(self) -> None:
        profile = resolve_profile("lisjong")
        summary = build_runtime_summary(
            profile, mode="ranked", trace_path=None, policy=profile.policy_factory()
        )
        text = format_runtime_summary(summary)
        self.assertNotIn("LISJONG_BOT_TOKEN", text)
        self.assertNotIn(_DUMMY_SECRET, text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Bearer", text)

    def test_summary_policy_label_reflects_the_actual_policy_instance_type(
        self,
    ) -> None:
        class _ExperimentalPolicy:
            def choose_action(self, decision):
                raise NotImplementedError

        profile = resolve_profile("lisjong-dev")
        summary = build_runtime_summary(
            profile, mode="ranked", trace_path=None, policy=_ExperimentalPolicy()
        )
        self.assertEqual(summary.policy_label, "_ExperimentalPolicy")
        self.assertIn("policy: _ExperimentalPolicy", format_runtime_summary(summary))


class MultiProfileIndependenceTest(unittest.TestCase):
    """別processから複数profileを同時起動しても構成が混線しないことの、
    実processを使わない最小確認(pure resolution/configurationの独立性)。
    """

    def test_resolving_two_profiles_does_not_share_mutable_state(self) -> None:
        dev_profile = resolve_profile("lisjong-dev")
        baseline_profile = resolve_profile("lisjong-baseline")

        self.assertNotEqual(
            dev_profile.credential_env_var, baseline_profile.credential_env_var
        )
        self.assertNotEqual(
            dev_profile.runtime_namespace, baseline_profile.runtime_namespace
        )

        env = {
            "LISJONG_DEV_BOT_TOKEN": "dev-secret",
            "LISJONG_BASELINE_BOT_TOKEN": "baseline-secret",
        }
        dev_token = resolve_credential(dev_profile, env=env)
        baseline_token = resolve_credential(baseline_profile, env=env)
        self.assertEqual(dev_token, "dev-secret")
        self.assertEqual(baseline_token, "baseline-secret")

    def test_runtime_profile_instances_are_independent_frozen_values(self) -> None:
        first = resolve_profile("lisjong-dev")
        second = resolve_profile("lisjong-dev")
        self.assertEqual(first, second)
        self.assertIsInstance(first, RuntimeProfile)


if __name__ == "__main__":
    unittest.main()
