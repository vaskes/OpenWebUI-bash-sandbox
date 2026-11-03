#!/usr/bin/env bash
# Mutation check for the browser door — browser_open, browser_screenshot,
# browser_close.
#
# A check that cannot be made red is a decoration. Each mutation below
# breaks one property on purpose; the named test must go red. If it
# stays green, the test is decoration and the property is unpinned.
#
# The properties this wave pins:
#
#   * the door is closed unless the operator listed the hosts the tool
#     may fetch, and a closed door says so;
#   * a host that is not on the list is refused — and refused before the
#     browser is reached at all;
#   * a page that REDIRECTS off the list is refused, and its content is
#     discarded. This is the property the measurement forced: Chromium
#     follows an HTTP redirect inside its own network stack and never
#     hands that follow-up to the route handler;
#   * the route handler aborts a navigation to a host that is not on the
#     list, and leaves the page's own subresources alone;
#   * every budget is a budget: the text and the screenshot are bounded,
#     and a truncated answer says so;
#   * the screenshot lands in the workspace through wave A's fence;
#   * the browser keeps no credentials: a session gets its own context,
#     and closing it drops that context;
#   * the door is counted — the counting is the audit trail;
#   * the catalogue and the schema are the contract the model sees.
#
# The mutations below also break the DETECTOR, and the control catches
# them. A verdict about "the property still holds" is a statement about
# the detector as much as about the property, and a detector that cannot
# see the thing it was written for would report a clean bill of health
# for a tree nobody broke.
set -u
cd "$(dirname "$0")/.."

. "$(dirname "$0")/mutation-lib.sh"
mutation_require_python

TESTS=tests/test_mcp_browser_v2250.py
WS_TESTS=tests/test_workspace_volume_check.py

# The tree has to be green before anything is broken: a mutation sweep
# against a red tree reports catches for a tree nobody broke.
mutation_require_green "$TESTS"

echo "== the door is open even with no list =="
mutate agent_browser.py \
  '    if not hosts:' \
  '    if False:' \
  "$TESTS::TestTheListIsTheOperators::test_an_empty_list_closes_the_door" \
  "the door is closed only when the operator says so"

echo "== a host that is not on the list is fetched anyway =="
mutate agent_browser.py \
  '    if not host_allowed(raw, hosts):' \
  '    if False:' \
  "$TESTS::TestTheListIsTheOperators::test_a_host_that_is_not_on_the_list_is_refused" \
  "the tool fetches a host the operator did not name"

echo "== a wildcard covers a lookalike =="
mutate agent_browser.py \
  '        return host == suffix[1:] or host.endswith(suffix)' \
  '        return host == suffix[1:]' \
  "$TESTS::TestTheListIsTheOperators::test_a_wildcard_covers_the_domain_and_what_is_under_it" \
  "a wildcard does not cover what is under the domain"

echo "== a scheme that is not http is accepted =="
mutate agent_browser.py \
  '    if scheme not in BROWSER_SCHEMES:' \
  '    if False:' \
  "$TESTS::TestTheListIsTheOperators::test_a_file_url_is_refused" \
  "the browser speaks any scheme at all"

echo "== a bare host is accepted =="
mutate agent_browser.py \
  '            raise BrowserRefusedError(
                f"the URL must be absolute and start with http:// or https:// — got {raw!r}"
            )' \
  '            pass' \
  "$TESTS::TestTheListIsTheOperators::test_a_bare_host_is_refused_with_the_fix" \
  "a URL whose meaning depends on a base is a URL"

echo "== a redirect off the list is followed =="
mutate agent_browser.py \
  '    if not host_allowed(final, hosts):' \
  '    if False:' \
  "$TESTS::TestThePageCannotWalkOffTheList::test_a_page_that_ends_up_elsewhere_is_refused" \
  "a page may walk off the list by redirecting"

echo "== the browser is reached before the host is checked =="
mutate agent_browser.py \
  '    checked = check_url(url, hosts)' \
  '    checked = str(url or "").strip()' \
  "$TESTS::TestThePageCannotWalkOffTheList::test_the_driver_is_not_reached_when_the_host_is_refused" \
  "the door asks the browser before it checks the host"

echo "== the text is not bounded =="
mutate agent_browser.py \
  '    text, truncated = bound_text(str(result.get("text") or ""), BROWSER_MAX_TEXT_BYTES)' \
  '    text, truncated = str(result.get("text") or ""), False' \
  "$TESTS::TestTheAnswerIsBounded::test_a_long_page_is_cut_and_says_so" \
  "the answer is whatever the page said"

echo "== the screenshot is not bounded =="
mutate agent_browser.py \
  '    if len(shot) > BROWSER_MAX_SCREENSHOT_BYTES:' \
  '    if False:' \
  "$TESTS::TestTheScreenshotGoesThroughTheFence::test_an_oversized_screenshot_is_refused" \
  "the screenshot budget does not apply"

echo "== the screenshot lands wherever the caller says =="
mutate agent_api.py \
  '    target = _safe_write_target(path)
    limit = model_write_limit()
    if len(payload) > limit:' \
  '    target = path
    limit = model_write_limit()
    if len(payload) > limit:' \
  "$TESTS::TestTheScreenshotGoesThroughTheFence::test_a_path_outside_the_workspace_is_refused" \
  "the screenshot is written outside the workspace"

echo "== a navigation off the list is not aborted =="
mutate agent_browser.py \
  '        if is_navigation and is_top and not host_allowed(request.url, self._allowed_hosts):' \
  '        if False:' \
  "$TESTS::TestTheRouteHandlerIsTheFence::test_a_navigation_to_a_host_that_is_not_on_the_list_is_aborted" \
  "the route handler lets any navigation through"

echo "== a subresource is treated as a navigation =="
mutate agent_browser.py \
  '        if is_navigation and is_top and not host_allowed(request.url, self._allowed_hosts):' \
  '        if not host_allowed(request.url, self._allowed_hosts):' \
  "$TESTS::TestTheRouteHandlerIsTheFence::test_a_subresource_is_the_page_s_own_business" \
  "the page may not load its own assets"

echo "== a session keeps its context after it is closed =="
mutate agent_browser.py \
  '        had = session_id in self._contexts' \
  '        had = True' \
  "$TESTS::TestTheBrowserKeepsNoCredentials::test_the_real_driver_says_so_too" \
  "closing a session claims a context that was never there"

echo "== the request is not counted =="
mutate agent_browser.py \
  '    _count("browser_requests_total")' \
  '    pass  # the counter is gone' \
  "$TESTS::TestTheDoorLeavesATrace::test_the_request_is_counted" \
  "the request leaves no trace"

echo "== a refusal is not counted =="
mutate mcp_server.py \
  '        METRICS.inc("browser_refused_total")' \
  '        pass  # the counter is gone' \
  "$TESTS::TestTheDoorLeavesATrace::test_a_refusal_is_counted" \
  "a refused request leaves no trace"

echo "== the tool takes something the model should not name =="
mutate mcp_server.py \
  'async def browser_open(url: str, session_id: str = DEFAULT_SESSION_ID) -> BrowserResult:' \
  'async def browser_open(url: str, session_id: str = DEFAULT_SESSION_ID, host: str | None = None) -> BrowserResult:' \
  "$TESTS::TestTheToolsAreTheDocumentedTools::test_the_open_schema_takes_a_url_and_a_session" \
  "the tool accepts more than a URL and a session"

echo "== the catalogue loses a tool =="
mutate mcp_server.py \
  '        "browser_open",' \
  '        "browser_opan",' \
  "$TESTS::TestTheToolsAreTheDocumentedTools::test_the_three_tools_are_in_the_catalogue" \
  "the catalogue is not the documented one"

echo "== the description stops stating the rules =="
mutate mcp_server.py \
  '        "absolute — it has to start with http:// or https:// — and its host "' \
  '        "absolute — and its host "' \
  "$TESTS::TestTheToolsAreTheDocumentedTools::test_the_description_states_the_rules" \
  "the model is not told what the door expects"

# The two mutations below break the DETECTOR, and the control catches them.
echo "== the detector cannot see a refusal off the list any more =="
mutate tests/test_mcp_browser_v2250.py \
  '        self.assertIn("cannot open the page", reason)
        self.assertIn("not on the list", reason)' \
  '        self.assertIn("cannot open the page", reason)
        self.assertIn("a phrase the detector can no longer see", reason)' \
  "$TESTS::TestTheDoorOverTheWire::test_the_tool_refuses_a_host_that_is_not_on_the_list" \
  "the detector cannot see the refusal it was written for"

echo "== the detector cannot see a redirect off the list any more =="
# The pattern has to name the redirect test and not the policy test: the
# phrase "elsewhere.example" is in both, and a mutation that is not
# unique changes the FIRST one — which is not where the detector looks.
mutate tests/test_mcp_browser_v2250.py \
  '        self.assertIn("cannot open the page", reason)
        self.assertIn("elsewhere.example", reason)' \
  '        self.assertIn("cannot open the page", reason)
        self.assertIn("a phrase the detector can no longer see", reason)' \
  "$TESTS::TestThePageCannotWalkOffTheList::test_a_page_that_ends_up_elsewhere_is_refused" \
  "the detector cannot see the redirect it was written for"

echo "== the driver asks for a browser the image does not carry =="
# The image installs one Chromium and skips the headless shell
# (--no-shell). Playwright's headless mode reaches for the shell when no
# channel is named, and then the binary is not there. This mutation takes
# the channel away; the test must go red.
mutate agent_browser.py \
  'BROWSER_LAUNCH = {"channel": "chromium"}' \
  'BROWSER_LAUNCH = {}' \
  "$TESTS::TestTheDriverLaunchesTheBrowserTheImageCarries::test_the_launch_asks_for_the_full_chromium" \
  "the driver asks for the headless shell the image does not carry"

echo "== the browser inherits the agent's environment =="
# Playwright's env replaces the environment rather than adding to it.
# The browser must not carry the agent's token: a same-uid sibling can
# read /proc/<pid>/environ. This mutation hands the whole environment
# to the browser; the test must go red.
mutate agent_browser.py \
  '                env=dict(BROWSER_ENV),' \
  '                env=dict(os.environ),' \
  "$TESTS::TestTheDriverLaunchesTheBrowserTheImageCarries::test_the_browser_does_not_inherit_the_agent_environment" \
  "the browser inherits the agent's environment, token and all"

echo "== the browser gets no writable config dir =="
# Chromium takes its home from the passwd entry and ignores $HOME, so
# on a read-only rootfs the crashpad handler cannot create its database
# and the browser dies at launch (SIGTRAP). This mutation points the
# config dir at the read-only home; the test must go red.
mutate agent_browser.py \
  '    "XDG_CONFIG_HOME": os.path.join(BROWSER_TMP_DIR, "config"),' \
  '    "XDG_CONFIG_HOME": "/home/agent/.config",' \
  "$TESTS::TestTheDriverLaunchesTheBrowserTheImageCarries::test_the_browser_gets_writable_dirs_on_the_tmpfs" \
  "the browser's config dir is on the read-only rootfs"

echo "== the workspace check waves a plain directory through =="
# The workspace is a filesystem in a file, mounted on the host. A volume
# that binds the mount POINT is not yet a workspace with a ceiling: when
# the mount is gone the path is a plain directory and the container
# starts on the host's partition. This mutation drops the filesystem
# check; the test must go red.
mutate tools/check-workspace-volume.sh \
  'if [ -e "$WS_PATH" ] && { [ -z "$WS_SRC" ] || [ "$WS_FS" != "ext4" ]; }; then' \
  'if false; then' \
  "$WS_TESTS::TestTheWorkspacePathMustBeAFilesystem::test_a_plain_directory_is_refused" \
  "the workspace check accepts a plain directory"

echo "== the workspace check stops saying what is wrong =="
mutate tools/check-workspace-volume.sh \
  'check-workspace-volume: a volume that binds a plain directory has no ceiling.' \
  'check-workspace-volume: something is off.' \
  "$WS_TESTS::TestTheWorkspacePathMustBeAFilesystem::test_the_refusal_says_what_is_missing" \
  "the refusal no longer says that the ceiling is missing"

echo "== the filesystem check comes after the early exit =="
# The script has an early exit for a deployment that has no workspace
# volume yet. If that exit comes first, a first deployment with an
# unmounted workspace starts a stack on the host's partition. This
# mutation writes the exit above the check; the test must go red.
mutate tools/check-workspace-volume.sh \
  'WS_SRC="$(findmnt -no SOURCE "$WS_PATH" 2>/dev/null || true)"' \
  'if [ -z "$volumes" ]; then
    echo "check-workspace-volume: there is no workspace volume yet."
    exit 0
fi
WS_SRC="$(findmnt -no SOURCE "$WS_PATH" 2>/dev/null || true)"' \
  "$WS_TESTS::TestTheWorkspacePathMustBeAFilesystem::test_the_filesystem_check_comes_before_the_early_exit" \
  "the early exit is written above the filesystem check"

echo
echo "mutations caught: $PASS, escaped: $FAIL"
[ "$FAIL" -eq 0 ]
