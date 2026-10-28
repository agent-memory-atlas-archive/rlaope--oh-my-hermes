"""An attached image is an `[Image #N]` token in the draft, and Hermes holds it exactly while a token names it.

The app is driven as `test_tui_modes` drives it: keys go through
`App._on_input` (or the decoder, for the bytes a terminal sends), the calls the
app made are read from the recording client, and Hermes' answers are handed
back with `App._on_response`. The composer's own rules for a token (atomic,
never typed by hand, wrapped whole) are `test_tui_composer.ImageTokenTests`.
"""

from __future__ import annotations

import unittest

from _tui_lines import lines_text
from test_tui_modes import SID, answer, make_app, notices, type_text
from omh.tui import gateway_contract as contract
from omh.tui.app import App, Attachment, QueuedLine
from omh.tui.keys import BACKSPACE, CHAR, ENTER, ESCAPE, LEFT, UP, Key, KeyDecoder, Paste
from omh.tui.theme import DEFAULT_THEME
from omh.tui.transcript import NOTICE_WARN, UserBlock, block_rows

SHOT = {"attached": True, "path": "/h/images/shot.png", "name": "shot.png", "width": 4, "height": 3}
NOTES = {"attached": True, "path": "/h/images/notes.png", "name": "notes.png", "width": 8, "height": 6}
CTRL_V = Key(CHAR, "v", ctrl=True)


def ctrl(char: str) -> Key:
    return Key(CHAR, char, ctrl=True)


def paste_image(app: App, client: object, result: dict) -> None:
    """Ctrl+V, and Hermes answers `clipboard.paste` with `result`."""
    app._on_input(CTRL_V)
    answer(app, client, result)  # type: ignore[arg-type]


def shown(app: App) -> str:
    return app.composer.expanded()


def image_calls(client: object) -> list[tuple[str, dict]]:
    return [(method, params) for method, params in client.calls if method.startswith("image.")]  # type: ignore[attr-defined]


def detach(path: str) -> tuple[str, dict]:
    return ("image.detach", {"session_id": SID, "path": path})


def attach(path: str) -> tuple[str, dict]:
    return ("image.attach", {"session_id": SID, "path": path})


def users(app: App) -> list[UserBlock]:
    return [block for block in app.transcript.blocks if isinstance(block, UserBlock)]


class TokenTests(unittest.TestCase):
    def test_a_pasted_image_is_a_token_at_the_caret_and_no_attachment_row(self) -> None:
        app, client = make_app()
        type_text(app, "이런느낌으로")
        for _ in range(len("느낌으로")):
            app._on_input(Key(LEFT))
        paste_image(app, client, SHOT)
        self.assertEqual(shown(app), "이런 [Image #1] 느낌으로")
        self.assertEqual([image.path for image in app.attachments], ["/h/images/shot.png"])
        app._render()
        screen = "\n".join(app.terminal.screen.lines())  # type: ignore[attr-defined]
        self.assertIn("❯ 이런 [Image #1] 느낌으로", screen)
        self.assertNotIn("📎", screen)
        self.assertNotIn("shot.png", screen)

    def test_images_are_numbered_through_the_session_like_claude_codes(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        paste_image(app, client, NOTES)
        self.assertEqual(shown(app), "[Image #1] [Image #2] ")
        app._on_input(Key(ENTER))
        paste_image(app, client, SHOT)
        self.assertEqual(shown(app), "[Image #3] ")  # a sent prompt does not start the count again

    def test_every_drop_and_attach_path_makes_a_token(self) -> None:
        for name, start, result in (
            ("/image", lambda app: app._image("shot.png what is this?"), {**SHOT, "remainder": "what is this?"}),
            ("Ctrl+V", lambda app: app._on_input(CTRL_V), SHOT),
            ("a dropped path", lambda app: app._on_input(Paste("/h/images/shot.png")),
             {**SHOT, "matched": True, "is_image": True}),
        ):
            with self.subTest(name):
                app, client = make_app()
                start(app)
                answer(app, client, result)
                self.assertTrue(shown(app).startswith("[Image #1] "), shown(app))
                self.assertEqual(shown(app), "[Image #1] " + result.get("remainder", ""))

    def test_typing_the_label_by_hand_attaches_nothing(self) -> None:
        app, client = make_app()
        type_text(app, "[Image #1] what is it")
        app._on_input(Key(ENTER))
        self.assertEqual(client.calls, [("prompt.submit", {"session_id": SID, "text": "[Image #1] what is it"})])
        self.assertEqual(users(app)[-1].images, ())


class DeletionTests(unittest.TestCase):
    """Whatever takes the token takes its image: Hermes is told to drop it, and no row is left behind."""

    def test_each_deletion_detaches_the_image(self) -> None:
        cmd_backspace = list(KeyDecoder().feed(b"\x15"))  # what iTerm2 and Ghostty send for Cmd+Backspace
        self.assertEqual(cmd_backspace, [ctrl("u")])
        cases = {
            "Backspace into the token": [Key(BACKSPACE)] * 3,  # the x, the space, the token
            "Cmd+Backspace (Ctrl+U)": cmd_backspace,
            "Ctrl+W": [ctrl("w"), ctrl("w")],
            "Ctrl+K from before it": [Key(LEFT), Key(LEFT), Key(LEFT), ctrl("k")],
            "Ctrl+C": [ctrl("c")],
            "Esc Esc": [Key(ESCAPE), Key(ESCAPE)],
        }
        for name, keys in cases.items():
            with self.subTest(name):
                app, client = make_app()
                type_text(app, "look")
                paste_image(app, client, SHOT)
                type_text(app, "x")
                for key in keys:
                    app._on_input(key)
                self.assertNotIn("[Image", shown(app))
                self.assertEqual(image_calls(client), [detach("/h/images/shot.png")])
                self.assertEqual(app.attachments, [])

    def test_the_decoded_bytes_of_cmd_backspace_take_the_text_and_the_image_together(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        type_text(app, "이런느낌으로")
        app._take_input(b"\x15")
        self.assertEqual((app.composer.text, app.attachments), ("", []))
        self.assertEqual(image_calls(client), [detach("/h/images/shot.png")])

    def test_undo_brings_the_token_back_and_hermes_holds_the_image_again(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        app._on_input(ctrl("u"))
        app._on_input(ctrl("_"))
        self.assertEqual(shown(app), "[Image #1] ")
        self.assertEqual(image_calls(client), [detach("/h/images/shot.png"), attach("/h/images/shot.png")])
        self.assertEqual([image.path for image in app.attachments], ["/h/images/shot.png"])

    def test_an_image_hermes_will_not_attach_again_is_said_once_and_never_asked_again(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        app._on_input(ctrl("u"))
        app._on_input(ctrl("_"))
        answer(app, client, error={"code": 4016, "message": "image not found"})
        type_text(app, "x")
        self.assertEqual(image_calls(client), [detach("/h/images/shot.png"), attach("/h/images/shot.png")])
        self.assertEqual(notices(app, NOTICE_WARN), ["[Image #1]: image not found (4016)."])


class SendTests(unittest.TestCase):
    def test_the_prompt_keeps_its_labels_and_the_echo_lists_each_image_under_it(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        type_text(app, "and ")
        paste_image(app, client, NOTES)
        type_text(app, "like this")
        app._on_input(Key(ENTER))
        self.assertEqual(client.calls[-1], ("prompt.submit", {"session_id": SID,
                                                              "text": "[Image #1] and [Image #2] like this"}))
        self.assertEqual(image_calls(client), [])  # Hermes held both: the prompt goes with them
        (user,) = users(app)
        self.assertEqual(user.images, ("[Image #1]", "[Image #2]"))
        rows = lines_text(block_rows(user, 60, DEFAULT_THEME))
        self.assertEqual([row.rstrip() for row in rows[-2:]], ["┃   ⎿  [Image #1]", "┃   ⎿  [Image #2]"])
        self.assertEqual(app.attachments, [])

    def test_a_queued_prompt_goes_with_its_images_and_never_with_the_drafts(self) -> None:
        app, client = make_app(busy=True)
        paste_image(app, client, SHOT)
        type_text(app, "first")
        app._on_input(Key(ENTER))  # queued behind the running turn
        (line,) = app.queued
        self.assertEqual([image.label for image in line.images], ["[Image #1]"])
        paste_image(app, client, NOTES)  # the next draft's
        before = len(client.calls)
        app.turn_pending = False
        app._turn_ended()
        sent = client.calls[before:]
        submit = next(index for index, (method, _params) in enumerate(sent) if method == "prompt.submit")
        self.assertEqual(sent[:submit], [detach("/h/images/notes.png")])
        self.assertEqual(sent[submit][1]["text"], "[Image #1] first")
        self.assertEqual(sent[submit + 1 :], [attach("/h/images/notes.png")])
        self.assertEqual(users(app)[-1].images, ("[Image #1]",))
        self.assertEqual([image.path for image in app.attachments], ["/h/images/notes.png"])

    def test_up_takes_a_queued_prompt_back_with_its_tokens(self) -> None:
        app, client = make_app(busy=True)
        paste_image(app, client, SHOT)
        type_text(app, "first")
        app._on_input(Key(ENTER))
        app._on_input(Key(UP))
        self.assertEqual(shown(app), "[Image #1] first")
        self.assertEqual(len(app.composer.images), 1)
        self.assertEqual(image_calls(client), [])  # Hermes held it all along

    def test_a_recalled_prompt_from_history_is_plain_text_and_attaches_nothing(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        type_text(app, "x")
        app._on_input(Key(ENTER))
        app.turn_pending = False
        app._on_input(Key(UP))
        self.assertEqual((app.composer.text, app.composer.images), ("[Image #1] x", {}))
        app._on_input(Key(ENTER))
        self.assertEqual(image_calls(client), [])
        self.assertEqual(users(app)[-1].images, ())

    def test_a_shell_line_goes_without_its_image_and_hermes_drops_it(self) -> None:
        app, client = make_app()
        type_text(app, "!ls ")
        paste_image(app, client, SHOT)
        app._on_input(Key(ENTER))
        self.assertEqual(image_calls(client), [detach("/h/images/shot.png")])
        self.assertEqual(app.attachments, [])

    def test_ctrl_enter_with_an_image_is_a_prompt_not_a_steer(self) -> None:
        app, client = make_app(busy=True)
        paste_image(app, client, SHOT)
        type_text(app, "this one")
        app._on_input(Key(ENTER, ctrl=True))
        self.assertEqual([method for method, _params in client.calls if method == "session.steer"], [])
        self.assertIsInstance(app.queued[-1], QueuedLine)
        self.assertEqual([image.label for image in app.queued[-1].images], ["[Image #1]"])

    def test_a_refused_prompt_leaves_no_image_held_without_a_token(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        type_text(app, "x")
        app._on_input(Key(ENTER))
        answer(app, client, error={"code": 4009, "message": "busy"})
        self.assertEqual(image_calls(client), [detach("/h/images/shot.png")])
        self.assertEqual(app.attachments, [])


class CommandTests(unittest.TestCase):
    """A command that sends a prompt (plan mode's `/plan`, a skill) sends it with the images; one that does not drops them."""

    def test_a_plan_mode_prompt_goes_with_its_images(self) -> None:
        app, client = make_app()
        app.plan_mode = True
        paste_image(app, client, SHOT)
        type_text(app, "plan this")
        app._on_input(Key(ENTER))
        self.assertEqual(client.calls[-1], ("slash.exec", {"command": "plan [Image #1] plan this", "session_id": SID}))
        answer(app, client, error={"code": contract.NOT_MINE, "message": "pending-input command"})
        answer(app, client, {"type": "send", "message": "Task to plan:\n[Image #1] plan this"})
        app._sync_images()
        self.assertEqual(client.calls[-1][0], "prompt.submit")
        self.assertEqual(image_calls(client), [])
        self.assertEqual(users(app)[-1].images, ("[Image #1]",))

    def test_a_command_that_only_prints_drops_the_images(self) -> None:
        app, client = make_app()
        type_text(app, "/usage ")
        paste_image(app, client, SHOT)
        app._on_input(Key(ENTER))
        answer(app, client, {"output": "12 calls"})
        app._sync_images()
        self.assertEqual(image_calls(client), [detach("/h/images/shot.png")])

    def test_a_prompt_held_for_the_default_check_keeps_its_images_held(self) -> None:
        app, client = make_app()
        app.default_route = ("m", "p")  # a read answered at launch: the next prompt waits for the check
        paste_image(app, client, SHOT)
        type_text(app, "x")
        app._on_input(Key(ENTER))
        self.assertIsNotNone(app.starting)
        app._sync_images()
        self.assertEqual(image_calls(client), [])
        app._default_settled()
        self.assertEqual(client.calls[-1][1]["text"], "[Image #1] x")
        self.assertEqual(users(app)[-1].images, ("[Image #1]",))


class AttachmentShapeTests(unittest.TestCase):
    def test_the_label_rides_on_the_attachment_the_composer_holds(self) -> None:
        app, client = make_app()
        paste_image(app, client, SHOT)
        ((label, image),) = app.composer.images.values()
        self.assertEqual(label, "[Image #1]")
        self.assertEqual(image, Attachment("/h/images/shot.png", "shot.png", "4×3", "[Image #1]"))


if __name__ == "__main__":
    unittest.main()
