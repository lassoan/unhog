import unittest

from unhog.app import file_manager_commands


class FileManagerCommandTests(unittest.TestCase):
    """The Linux "Open in file manager" fallbacks: D-Bus FileManager1 first, xdg-open last."""

    def test_open_folder(self):
        cmds = file_manager_commands("/home/me/Pictures/Camera Roll", select=False)
        self.assertEqual([c[0] for c in cmds], ["gdbus", "dbus-send", "xdg-open"])
        self.assertIn("org.freedesktop.FileManager1.ShowFolders", cmds[0])
        self.assertIn("['file:///home/me/Pictures/Camera%20Roll']", cmds[0])
        self.assertIn("array:string:file:///home/me/Pictures/Camera%20Roll", cmds[1])
        self.assertEqual(cmds[2], ["xdg-open", "/home/me/Pictures/Camera Roll"])

    def test_reveal_item_opens_parent(self):
        cmds = file_manager_commands("/home/me/big.iso", select=True)
        self.assertIn("org.freedesktop.FileManager1.ShowItems", cmds[0])
        self.assertIn("['file:///home/me/big.iso']", cmds[0])
        self.assertEqual(cmds[-1], ["xdg-open", "/home/me"])

    def test_quotes_in_names_cannot_break_the_gdbus_argument(self):
        cmds = file_manager_commands("/home/me/it's here", select=False)
        self.assertIn("['file:///home/me/it%27s%20here']", cmds[0])


if __name__ == "__main__":
    unittest.main()
