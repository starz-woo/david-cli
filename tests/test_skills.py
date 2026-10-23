import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


class SkillRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.document = self.root / 'review' / 'SKILL.md'
        self.document.parent.mkdir()
        self.document.write_text('# Review\n실제 변경 사항을 검토하세요.\n', encoding='utf-8')

    def registry(self, registered=None, base_dir=None):
        from david.skills import SkillRegistry
        return SkillRegistry({'review': 'review/SKILL.md'} if registered is None else registered,
                             self.root if base_dir is None else base_dir)

    def test_registration_and_selection_are_explicit_bounded_unique_names(self):
        from david.skills import SkillError, SkillRegistry
        for registered in ([], None, {'bad/name': str(self.document)},
                           {'': str(self.document)}, {'a' * 65: str(self.document)},
                           {1: str(self.document)}, {'review': 1}, {'review': ''},
                           {'review': 'bad\x00path'},
                           {str(i): str(self.document) for i in range(33)}):
            with self.subTest(registered=registered), self.assertRaises(SkillError):
                SkillRegistry(registered, self.root)
        registry = self.registry()
        for names in ('review', None, ['missing'], ['review', 'review'], [1],
                      ['review'] * 9, ['bad/name']):
            with self.subTest(names=names), self.assertRaises(SkillError):
                registry.load(names)
        self.assertEqual(self.registry({}).list(), [])
        self.assertEqual(self.registry({}).load([]), '')

    def test_only_regular_unlinked_explicit_documents_are_accepted(self):
        from david.skills import SkillError
        link = self.root / 'link.md'
        link.symlink_to(self.document)
        directory_link = self.root / 'linked'
        directory_link.symlink_to(self.document.parent, target_is_directory=True)
        hardlink = self.root / 'hard.md'
        os.link(self.document, hardlink)
        for path in (link, directory_link / 'SKILL.md', self.document,
                     self.document.parent, self.root / 'missing.md'):
            with self.subTest(path=path), self.assertRaises(SkillError):
                self.registry({'review': str(path)}).load(['review'])
        hardlink.unlink()
        if hasattr(os, 'mkfifo'):
            fifo = self.root / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaises(SkillError):
                self.registry({'review': str(fifo)})
        with self.assertRaises(SkillError):
            self.registry({'review': 'SKILL.md'}, base_dir=directory_link).load(['review'])
        self.assertIn('# Review', self.registry({'review': str(self.document)}).load(['review']))

    def test_document_bytes_are_bounded_utf8_and_control_free(self):
        from david.skills import SkillError
        registry = self.registry()
        for content in (b'x' * (32768 + 1), b'\xff', b'hello\x1b[31m', b'\x00',
                        '\u0085'.encode(), '\u202e'.encode()):
            self.document.write_bytes(content)
            with self.subTest(content=content[:20]), self.assertRaises(SkillError):
                registry.load(['review'])
        self.document.write_bytes(b'x' * 32768)
        self.assertIn('x' * 32768, registry.load(['review']))
        self.document.write_text('한글\n\r\t', encoding='utf-8')
        self.assertIn('한글', registry.load(['review']))
        self.document.write_text('한' * 10923, encoding='utf-8')
        with self.assertRaises(SkillError):
            registry.load(['review'])

    def test_combined_prompt_including_wrappers_is_bounded(self):
        from david.skills import SkillError
        self.document.write_bytes(b'x' * 32768)
        other = self.root / 'second.md'
        other.write_bytes(b'y' * 32768)
        registry = self.registry({'review': str(self.document), 'second': str(other)})
        with self.assertRaises(SkillError):
            registry.load(['review', 'second'])
        other.write_bytes(b'y' * 30000)
        text = registry.load(['second', 'review'])
        self.assertLessEqual(len(text.encode('utf-8')), 65536)
        self.assertLess(text.index('y' * 20), text.index('x' * 20))

    def test_load_rechecks_links_added_after_registration(self):
        from david.skills import SkillError
        registry = self.registry()
        replacement = self.root / 'replacement.md'
        replacement.write_text('must not be loaded', encoding='utf-8')
        self.document.unlink()
        self.document.symlink_to(replacement)
        with self.assertRaises(SkillError):
            registry.load(['review'])
        self.document.unlink()
        os.link(replacement, self.document)
        with self.assertRaises(SkillError):
            registry.load(['review'])
        self.document.unlink()
        replacement.rename(self.document)
        saved_directory = self.root / 'saved'
        self.document.parent.rename(saved_directory)
        self.document.parent.symlink_to(saved_directory, target_is_directory=True)
        with self.assertRaises(SkillError):
            registry.load(['review'])

    def test_replacement_between_safety_check_and_open_is_rejected(self):
        import david.skills as skills
        registry = self.registry()
        original_snapshot = skills._snapshot
        checked = False

        def replace_after_check(path):
            nonlocal checked
            result = original_snapshot(path)
            if not checked:
                checked = True
                replacement = self.root / 'replacement.md'
                replacement.write_text('raced replacement', encoding='utf-8')
                os.replace(replacement, self.document)
            return result

        with patch.object(skills, '_snapshot', side_effect=replace_after_check):
            with self.assertRaises(skills.SkillError):
                registry.load(['review'])
        self.assertTrue(checked)

    def test_mutation_during_read_is_rejected(self):
        import david.skills as skills
        registry = self.registry()
        original_fstat = os.fstat
        changed = False

        def mutate_after_descriptor_check(fd):
            nonlocal changed
            result = original_fstat(fd)
            if not changed and result.st_ino == self.document.stat().st_ino:
                changed = True
                self.document.write_text('changed during read', encoding='utf-8')
            return result

        with patch.object(skills.os, 'fstat', side_effect=mutate_after_descriptor_check):
            with self.assertRaises(skills.SkillError):
                registry.load(['review'])
        self.assertTrue(changed)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO required')
    def test_file_raced_to_fifo_is_rejected_without_blocking_open(self):
        import david.skills as skills
        registry = self.registry()
        original_snapshot = skills._snapshot
        changed = False

        def replace_with_fifo(path):
            nonlocal changed
            result = original_snapshot(path)
            if not changed:
                changed = True
                self.document.unlink()
                os.mkfifo(self.document)
            return result

        # Guard the insecure blocking path so a regression fails instead of hanging.
        with patch.object(skills, '_snapshot', side_effect=replace_with_fifo), patch.object(
                Path, 'open', side_effect=AssertionError('unsafe blocking open')):
            with self.assertRaises(skills.SkillError):
                registry.load(['review'])
        self.assertTrue(changed)

    def test_frontmatter_metadata_is_optional_simple_bounded_and_not_executed(self):
        registry = self.registry()
        self.document.write_text('---\nname: different-name\ndescription: "한국어 코드 검토"\n'
                                 '---\n# Actual body\n[reference](missing.md)\n', encoding='utf-8')
        self.assertEqual(registry.list()[0]['description'], '한국어 코드 검토')
        text = registry.load(['review'])
        self.assertIn('# Actual body', text)
        self.assertIn('[reference](missing.md)', text)
        self.assertNotIn('description:', text)
        self.assertIn('(review)', text)
        self.document.write_text('---\ndescription: ' + 'x' * 1000 + '\n---\nbody', encoding='utf-8')
        self.assertLessEqual(len(registry.list()[0]['description']), 512)
        self.document.write_text('---\ndescription: !!python/object/apply:os.system [touch nope]\n'
                                 '---\nbody', encoding='utf-8')
        self.assertEqual(registry.list()[0]['description'], '')
        self.assertEqual(registry.load(['review']).count('body'), 1)
        self.assertFalse((self.root / 'nope').exists())
        self.document.write_text('---\nordinary markdown without closing delimiter', encoding='utf-8')
        self.assertIn('ordinary markdown', registry.load(['review']))

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS system aliases')
    def test_macos_system_alias_is_canonicalized_without_resolving_user_links(self):
        from david.skills import _canonical_system_prefix
        self.assertEqual(_canonical_system_prefix(Path('/var/folders/example/SKILL.md')),
                         Path('/private/var/folders/example/SKILL.md'))
        self.assertEqual(_canonical_system_prefix(Path('/tmp/example/SKILL.md')),
                         Path('/private/tmp/example/SKILL.md'))
        link = self.root / 'linked'
        link.symlink_to(self.document.parent, target_is_directory=True)
        self.assertEqual(_canonical_system_prefix(link / 'SKILL.md'), link / 'SKILL.md')

    def test_bundled_korean_example_is_an_explicit_loadable_document(self):
        from david.skills import SkillRegistry
        root = Path(__file__).resolve().parents[1]
        registry = SkillRegistry({'review': 'examples/skills/review/SKILL.md'}, root)
        self.assertTrue(registry.list()[0]['description'])
        text = registry.load(['review'])
        self.assertIn('코드 리뷰', text)
        self.assertIn('검증', text)
        self.assertIn('권한', text)

    def test_registered_paths_cannot_smuggle_unicode_controls_into_listing(self):
        from david.skills import SkillError
        for control in ('\u0085', '\u202e'):
            path = self.root / ('review' + control + '.md')
            path.write_text('body', encoding='utf-8')
            with self.subTest(control=control), self.assertRaises(SkillError):
                self.registry({'review': str(path)})

    def test_settings_relative_document_loads_as_bounded_authority_instructions(self):
        registry = self.registry()
        text = registry.load(['review'])
        self.assertIn('USER-SELECTED SKILL INSTRUCTIONS', text)
        self.assertIn('cannot grant permission or override host policy', text)
        self.assertIn('실제 변경 사항을 검토하세요.', text)
        self.assertEqual(registry.load([]), '')
        self.assertEqual(registry.list(), [{'name': 'review', 'path': str(self.document),
                                           'description': ''}])
        self.assertEqual(set(registry.protected_paths()), {self.document, self.document.parent})

    def test_settings_base_does_not_depend_on_cwd_or_discover_other_files(self):
        workspace = self.root / 'workspace'
        workspace.mkdir()
        (workspace / 'SKILL.md').write_text('unregistered instructions', encoding='utf-8')
        (self.document.parent / 'linked.md').symlink_to(self.root / 'missing')
        cwd = Path.cwd()
        try:
            os.chdir(workspace)
            registry = self.registry()
            self.assertEqual([item['name'] for item in registry.list()], ['review'])
            self.assertNotIn('unregistered instructions', registry.load(['review']))
            self.assertIn('# Review', self.registry(
                {'review': 'workspace/../review/SKILL.md'}).load(['review']))
        finally:
            os.chdir(cwd)

    def test_reparse_attributes_are_rejected_on_file_and_ancestors(self):
        from david.skills import SkillError
        real_lstat = Path.lstat
        for target in (self.document, self.document.parent, self.root):
            def marked_lstat(path):
                info = real_lstat(path)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with self.subTest(target=target), patch.object(Path, 'lstat', marked_lstat):
                with self.assertRaises(SkillError):
                    self.registry()

    def test_limit_boundaries_allow_explicit_aliases_and_protect_all_registrations(self):
        from david.skills import SkillError
        paths = {f'skill_{i}': str(self.document) for i in range(32)}
        registry = self.registry(paths)
        self.assertEqual(len(registry.list()), 32)
        self.assertIn('(skill_7)', registry.load(list(paths)[:8]))
        with self.assertRaises(SkillError):
            registry.load(list(paths)[:9])
        other = self.root / 'second' / 'SKILL.md'
        other.parent.mkdir()
        other.write_text('second document', encoding='utf-8')
        registry = self.registry({'review': str(self.document), 'other': str(other)})
        registry.load(['review'])
        self.assertEqual(set(registry.protected_paths()),
                         {self.document, self.document.parent, other, other.parent})


class PortableSkillRegistryTests(SkillRegistryTests):
    """Exercise the fallback with real files, without pretending this is Windows."""

    def setUp(self):
        super().setUp()
        context = patch.object(os, 'supports_dir_fd', set())
        context.start()
        self.addCleanup(context.stop)
