import unittest
from scripts.bootstrap import public_key


class PublicKeyTest(unittest.TestCase):
    def test_ssh_keygen_comment_is_not_part_of_authorization(self):
        self.assertEqual(public_key("ssh-ed25519 AAAA= user@host.local\n"), "ssh-ed25519 AAAA=")
        self.assertEqual(public_key("ssh-ed25519 AAAA= arbitrary comment"), "ssh-ed25519 AAAA=")

    def test_key_options_and_extra_lines_are_rejected(self):
        for value in ('command="sh" ssh-ed25519 AAAA=', "ssh-ed25519 AAAA=\nssh-ed25519 BBBB=",
                      "ssh-rsa AAAA= user@host"):
            with self.assertRaises(ValueError):
                public_key(value)
