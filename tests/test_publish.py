"""Publication, retries, and draft/form contracts; no live Moodle writes."""

import contextlib
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from pypdf import PdfReader

from plm_moodle_sync.cli import main
from plm_moodle_sync.config import load_config
from plm_moodle_sync.moodle.client import MoodleClient
from plm_moodle_sync.moodle.errors import MoodleError
from plm_moodle_sync.moodle.publish import FilePublisher
from plm_moodle_sync.state import load_state, save_state, state_lock, StateError
from plm_moodle_sync.sync import upload_configured
from test_moodle import COOKIE, DASHBOARD, SERVER, response
from test_pdf import make_pdf


PDF = b'%PDF-1.4\nexample PDF bytes for unit tests\n%%EOF'
DIGEST = sha256(PDF).hexdigest()
PROJECT = 'a' * 24
FORM = DASHBOARD + '''
<form action="/instance/course/modedit.php" method="post">
<input name="_qf__mod_resource_mod_form" value="1">
<input name="course" value="301"><input name="coursemodule" value="77">
<input name="files" value="123"><input name="name" value="Old name">
<textarea name="introeditor[text]">Description</textarea>
<textarea name="availabilityconditionsjson">{"op":"&amp;"}</textarea>
<select name="visible"><option value="0" selected>Hidden</option><option value="1">Visible</option></select>
<input type="radio" name="completion" value="0"><input type="radio" name="completion" value="1" checked>
<input name="disabledsetting" value="ignored" disabled>
<input name="coursecontentnotification" type="checkbox" value="1" checked>
<input name="submitbutton2" type="submit" value="Save"><input name="cancel" type="submit" value="Cancel">
</form><script>M.form_filemanager.init(Y, {"itemid":123,"maxbytes":1000000,
"context":{"id":987},"filepicker":{"repositories":{"9":{"type":"upload","id":9}}}});</script>
'''


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.client = MoodleClient(SERVER, COOKIE)
        self.addCleanup(self.client.session.close)
        self.publisher = FilePublisher(self.client)
        self.file = {'filename': 'TD.pdf', 'filepath': '/'}

    def route(self, replies):
        pending = iter(replies)

        def request(method, url, **kwargs):
            reply = next(pending)
            if isinstance(reply, Exception):
                raise reply
            reply.url = url
            if 'files' in kwargs:
                self.upload = kwargs['files']['repo_upload_file'][:1] + (kwargs['files']['repo_upload_file'][1].read(),)
            return reply

        self.request = Mock(side_effect=request)
        self.client.session.request = self.request

    def replies(self, *, update=False, final=None):
        return [response(FORM), response(data={'list': [self.file] if update else []}),
                *([response(data={'filepath': '/'})] if update else []),
                response(data={'id': 123, 'file': 'TD.pdf'}), response(data={'list': [self.file]}),
                response(data=True), final or response(status=303, location=SERVER + '/course/view.php?id=301')]

    def test_create_uses_form_ids_exact_filename_and_explicit_mainfile(self):
        self.route(self.replies())
        before = Mock()
        self.publisher.publish(301, 6, 'TD.pdf', PDF, before_submit=before)
        calls = self.request.call_args_list
        self.assertEqual(calls[0].kwargs['params']['section'], 6)
        self.assertEqual(calls[2].kwargs['data']['itemid'], '123')
        self.assertEqual(calls[2].kwargs['data']['repo_id'], 9)
        self.assertEqual(calls[2].kwargs['data']['ctx_id'], 987)
        self.assertEqual(self.upload, ('TD.pdf', PDF))
        self.assertEqual(calls[4].kwargs['data']['action'], 'setmainfile')
        before.assert_called_once()

    def test_replace_preserves_settings_and_only_deletes_private_draft(self):
        self.route(self.replies(update=True))
        self.publisher.publish(301, 6, 'TD.pdf', PDF, cmid=77)
        calls = self.request.call_args_list
        self.assertEqual(calls[0].kwargs['params'], {'update': 77})
        self.assertEqual(calls[2].args[1], SERVER + '/repository/draftfiles_ajax.php')
        self.assertEqual(calls[2].kwargs['data']['action'], 'delete')
        fields = calls[-1].kwargs['data']
        self.assertEqual(fields['coursemodule'], '77')
        self.assertEqual(fields['introeditor[text]'], 'Description')
        self.assertEqual(fields['availabilityconditionsjson'], '{"op":"&"}')
        self.assertEqual(fields['visible'], '0')
        self.assertEqual(fields['completion'], '1')
        for field in ('cancel', 'disabledsetting', 'coursecontentnotification'):
            self.assertNotIn(field, fields)

    def test_display_name_is_submitted_separately_from_download_filename(self):
        self.route(self.replies(update=True))
        self.publisher.publish(301, 6, 'TD.pdf', PDF, cmid=77, name='TD1')
        self.assertEqual(self.upload[0], 'TD.pdf')
        self.assertEqual(self.request.call_args_list[-1].kwargs['data']['name'], 'TD1')

    def test_visibility_override_when_creating_or_replacing_pdf(self):
        for cmid in (None, 77):
            for visible in (True, False):
                with self.subTest(cmid=cmid, visible=visible):
                    replies = self.replies(update=cmid is not None)
                    if not visible:
                        replies[0] = response(FORM.replace('value="0" selected>Hidden', 'value="0">Hidden')
                                              .replace('value="1">Visible', 'value="1" selected>Visible'))
                    self.route(replies)
                    self.publisher.publish(301, 6, 'TD.pdf', PDF, cmid=cmid, visible=visible)
                    self.assertEqual(self.request.call_args.kwargs['data']['visible'], str(int(visible)))

    def test_settings_update_retains_pdf_and_other_fields_without_uploading(self):
        for visible, expected in ((True, '1'), (False, '0'), (None, '0')):
            with self.subTest(visible=visible):
                self.route([response(FORM), response(status=303, location=SERVER + '/course/view.php?id=301')])
                pending = Mock()
                self.publisher.publish(301, 6, 'TD.pdf', PDF, cmid=77, name='TD1',
                                       visible=visible, replace_pdf=False, before_submit=pending)
                self.assertEqual(self.request.call_count, 2)
                fields = self.request.call_args.kwargs['data']
                self.assertEqual(fields['files'], '123')
                self.assertEqual(fields['visible'], expected)
                self.assertEqual(fields['name'], 'TD1')
                self.assertEqual(fields['introeditor[text]'], 'Description')
                self.assertEqual(fields['completion'], '1')
                self.assertNotIn('files', self.request.call_args.kwargs)
                pending.assert_called_once()

    def test_unavailable_visibility_override_stops_before_upload_or_save(self):
        self.route([response(FORM.replace('name="visible"', 'name="unavailable"'))])
        with self.assertRaisesRegex(MoodleError, 'visibility setting is unavailable'):
            self.publisher.publish(301, 6, 'TD.pdf', PDF, cmid=77, visible=True)
        self.assertEqual(self.request.call_count, 1)

    def test_unexpected_draft_attachments_are_not_removed(self):
        self.route([response(FORM), response(data={'list': [self.file, {'filename': 'extra.pdf'}]})])
        with self.assertRaisesRegex(MoodleError, 'unexpected files'):
            self.publisher.publish(301, 6, 'TD.pdf', PDF, cmid=77)
        self.assertEqual(self.request.call_count, 2)

    def test_oversized_pdf_stops_before_draft_requests(self):
        self.route([response(FORM.replace('1000000', '10'))])
        with self.assertRaisesRegex(MoodleError, 'size limit'):
            self.publisher.publish(301, 6, 'TD.pdf', PDF)
        self.assertEqual(self.request.call_count, 1)

    def test_missing_library_reports_python_environment_before_network_access(self):
        self.route([])
        with patch.dict('sys.modules', {'py_moodle.module': None}), self.assertRaisesRegex(MoodleError, 'active Python environment'):
            self.publisher.publish(301, 6, 'TD.pdf', PDF)
        self.request.assert_not_called()

    def test_upload_failure_or_renaming_does_not_submit_form(self):
        for result in ({'error': 'secret'}, {'event': 'fileexists'}, {'id': 123, 'file': 'TD(1).pdf'}):
            self.route([response(FORM), response(data={'list': []}), response(data=result)])
            with self.subTest(result=result), self.assertRaises(MoodleError) as error:
                self.publisher.publish(301, 6, 'TD.pdf', PDF)
            self.assertNotIn('secret', str(error.exception))
            self.assertEqual(self.request.call_count, 3)

    def test_save_failure_persists_intent_and_never_blindly_retries(self):
        for final in (requests.Timeout('secret'), response('secret', status=200),
                      response(status=302, location='https://cas.example/?ticket=secret')):
            self.route(self.replies(final=final))
            before = Mock()
            with self.subTest(final=final), self.assertRaises(MoodleError) as error:
                self.publisher.publish(301, 6, 'TD.pdf', PDF, before_submit=before)
            before.assert_called_once()
            self.assertNotIn('secret', str(error.exception))
            self.assertEqual(self.request.call_count, 6)

    def test_download_filename_mismatch_reports_names_without_url_secrets(self):
        pdf = response()
        pdf.content = PDF
        self.route([
            response(status=303, location=SERVER +
                     '/pluginfile.php/88/mod_resource/content/1/Other.pdf?token=secret'),
            pdf,
        ])
        with self.assertRaisesRegex(MoodleError, "returned filename 'Other.pdf'; expected 'TD.pdf'") as error:
            self.publisher.pdf_hash(77, 'TD.pdf')
        self.assertNotIn('secret', str(error.exception))

    def test_download_rejects_non_pdf_content_at_the_expected_filename(self):
        link = SERVER + '/pluginfile.php/88/mod_resource/content/1/TD.pdf'
        html = response('<a href="' + link + '">Download</a>')
        html.content = html.text.encode()
        body = response()
        body.content = b'Not a PDF'
        self.route([html, body])
        with self.assertRaisesRegex(MoodleError, 'did not return valid PDF content'):
            self.publisher.pdf_hash(77, 'TD.pdf')

    def test_download_follows_same_site_resource_link_and_checks_filename(self):
        link = SERVER + '/pluginfile.php/88/mod_resource/content/1/TD.pdf'
        pdf = response()
        pdf.content = PDF
        self.route([response('<a href="' + link + '">Download</a>'), pdf])
        # Initial HTML does not look like a PDF.
        original = self.request.side_effect

        def route(method, url, **kwargs):
            reply = original(method, url, **kwargs)
            if '/view.php' in url:
                reply.content = reply.text.encode()
            return reply

        self.request.side_effect = route
        self.assertEqual(self.publisher.pdf_hash(77, 'TD.pdf'), DIGEST)
        self.assertEqual(self.request.call_count, 2)

    def test_foreign_download_link_is_rejected(self):
        html = response('<a href="https://other.example/pluginfile.php/88/mod_resource/content/1/TD.pdf">PDF</a>')
        html.content = html.text.encode()
        self.route([html])
        with self.assertRaisesRegex(MoodleError, 'Cannot locate'):
            self.publisher.pdf_hash(77, 'TD.pdf')
        self.assertEqual(self.request.call_count, 1)


class PublicationStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        path = self.root / 'sync.yaml'
        path.write_text('plmlatex:\n  server: https://plm.example\n  project_id: "' + PROJECT
                        + '"\nmoodle:\n  server: ' + SERVER + '\n  course_id: 301\nfiles:\n'
                        + ''.join(f'  - source:\n      tex: TD/{name}.tex\n    target:\n      section: Test\n      name: {name}.pdf\n'
                                  for name in ('TD1', 'TD2')))
        self.config = load_config(path)
        self.state = {'version': 2, 'projects': {'https://plm.example/project/' + PROJECT: {'sources': {'keep': {}}, 'builds': {}}}}
        for name in ('TD1', 'TD2'):
            pdf_path = self.config.sync.cache_dir / PROJECT / 'TD' / (name + '.pdf')
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(PDF)
            self.state['projects']['https://plm.example/project/' + PROJECT]['builds']['TD/' + name + '.tex'] = {
                'pdf': 'TD/' + name + '.pdf', 'sha256': DIGEST}
        save_state(self.config.sync.state_file, self.state)
        self.modules = []
        self.hashes = {}
        self.visibilities = {}
        self.writes = []
        self.fail_after_save = False
        self.fail_file = None
        self.client = Mock(server=SERVER)
        self.client.__enter__ = Mock(return_value=self.client)
        self.client.__exit__ = Mock(return_value=False)
        self.client.course.side_effect = self.course
        self.client.resource_visibility.side_effect = lambda course_id, cmid: self.visibilities[cmid]
        self.publisher = Mock()
        self.publisher.pdf_hash.side_effect = lambda cmid, filename: self.hashes[cmid]
        self.publisher.publish.side_effect = self.publish
        for patcher in (patch('plm_moodle_sync.sync.MoodleClient', return_value=self.client),
                        patch('plm_moodle_sync.sync.FilePublisher', return_value=self.publisher),
                        patch('plm_moodle_sync.sync.moodle_auth.load_cookies', return_value=COOKIE)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def course(self, course_id):
        return {'sections': [{'id': 20182, 'number': 6, 'name': 'Test', 'modules': deepcopy(self.modules)}]}

    def publish(self, course_id, number, filename, content, *, cmid, name, visible=None, replace_pdf=True, before_submit):
        self.assertEqual(number, 6)
        if filename == self.fail_file:
            raise MoodleError('Draft failed')
        before_submit()
        if cmid is None:
            cmid = 100 + len(self.modules)
            self.modules.append({'id': cmid, 'name': filename, 'modname': 'resource'})
            self.visibilities[cmid] = 1
        next(module for module in self.modules if module['id'] == cmid)['name'] = name
        if replace_pdf:
            self.hashes[cmid] = sha256(content).hexdigest()
        if visible is not None:
            self.visibilities[cmid] = int(visible)
        self.writes.append(cmid)
        if self.fail_after_save:
            raise MoodleError('Save response lost')

    def run_upload(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return upload_configured(self.config, **kwargs)

    def records(self):
        return load_state(self.config.sync.state_file).get('publications', {})

    def test_create_skip_and_force_replacement_preserve_resource_ids(self):
        self.assertEqual([r['action'] for r in self.run_upload()], ['create', 'create'])
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
        self.assertEqual(self.writes, [100, 101])
        self.assertEqual([r['action'] for r in self.run_upload(force=True)], ['update', 'update'])
        self.assertEqual(self.writes, [100, 101, 100, 101])
        self.assertEqual(load_state(self.config.sync.state_file)['projects'], self.state['projects'])
        self.assertEqual(len(self.records()), 2)

    def test_changed_build_updates_existing_resource(self):
        self.run_upload()
        state = load_state(self.config.sync.state_file)
        changed = PDF + b'\nnew build'
        (self.config.sync.cache_dir / PROJECT / 'TD/TD1.pdf').write_bytes(changed)
        state['projects']['https://plm.example/project/' + PROJECT]['builds']['TD/TD1.tex']['sha256'] = sha256(changed).hexdigest()
        save_state(self.config.sync.state_file, state)
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])
        self.assertEqual(self.writes, [100, 101, 100])

    def set_first_name(self, name):
        first, *others = self.config.files
        self.config = replace(self.config, files=(replace(first, target=replace(first.target, name=name)), *others))

    def set_first_section(self, section):
        first, *others = self.config.files
        self.config = replace(self.config, files=(replace(first, target=replace(first.target, section=section)), *others))

    def set_first_visible(self, visible):
        first, *others = self.config.files
        self.config = replace(self.config, files=(replace(first, target=replace(first.target, visible=visible)), *others))

    def set_first_pages(self, pages):
        first, *others = self.config.files
        self.config = replace(self.config, files=(replace(first, target=replace(first.target, pages=pages)), *others))

    def replace_cached_pdf(self, name, content):
        state = load_state(self.config.sync.state_file)
        path = self.config.sync.cache_dir / PROJECT / 'TD' / (name + '.pdf')
        path.write_bytes(content)
        state['projects']['https://plm.example/project/' + PROJECT]['builds']['TD/' + name + '.tex']['sha256'] = sha256(content).hexdigest()
        save_state(self.config.sync.state_file, state)
        return path

    def test_pages_publish_selected_bytes_keep_full_cache_and_skip_repeat(self):
        original = make_pdf(['one', 'two', 'three'])
        path = self.replace_cached_pdf('TD1', original)
        self.set_first_pages('1-2')
        reports = self.run_upload()
        output = self.publisher.publish.call_args_list[0].args[3]
        self.assertEqual([page.extract_text() for page in PdfReader(io.BytesIO(output)).pages], ['one', 'two'])
        self.assertEqual(next(iter(self.records().values()))['sha256'], sha256(output).hexdigest())
        self.assertNotEqual(sha256(output).hexdigest(), sha256(original).hexdigest())
        self.assertEqual(reports[0]['pages'], '1-2')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
        self.assertEqual(self.writes, [100, 101])

    def test_page_range_change_and_removal_update_same_resource_without_changing_source(self):
        original = make_pdf(['one', 'two', 'three'])
        path = self.replace_cached_pdf('TD1', original)
        self.set_first_pages('1-2')
        self.run_upload()
        self.set_first_pages('2-3')
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])
        self.assertEqual(self.writes, [100, 101, 100])
        output = self.publisher.publish.call_args.args[3]
        self.assertEqual([page.extract_text() for page in PdfReader(io.BytesIO(output)).pages], ['two', 'three'])
        self.set_first_pages(None)
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])
        self.assertEqual(self.publisher.publish.call_args.args[3], original)
        self.assertEqual(path.read_bytes(), original)

    def test_only_changes_in_selected_pages_trigger_upload(self):
        self.replace_cached_pdf('TD1', make_pdf(['one', 'two', 'three']))
        self.set_first_pages('1-2')
        self.run_upload()
        self.replace_cached_pdf('TD1', make_pdf(['one', 'two', 'changed']))
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
        self.replace_cached_pdf('TD1', make_pdf(['changed', 'two', 'changed']))
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])

    def test_invalid_later_page_range_stops_before_moodle_authentication(self):
        self.replace_cached_pdf('TD2', make_pdf(['one', 'two']))
        first, second = self.config.files
        self.config = replace(self.config, files=(first, replace(second, target=replace(second.target, pages='1-11'))))
        before = self.config.sync.state_file.read_bytes()
        with self.assertRaisesRegex(ValueError, 'TD/TD2.tex.*only 2 pages'):
            self.run_upload()
        self.client.__enter__.assert_not_called()
        self.assertEqual(self.config.sync.state_file.read_bytes(), before)
        self.assertEqual(self.writes, [])

    def test_page_extraction_dry_run_preserves_full_cache_and_state(self):
        original = make_pdf(['one', 'two', 'three'])
        path = self.replace_cached_pdf('TD1', original)
        self.set_first_pages('2')
        before = self.config.sync.state_file.read_bytes()
        reports = self.run_upload(dry_run=True)
        self.assertEqual(reports[0]['pages'], '2')
        self.assertEqual(self.config.sync.state_file.read_bytes(), before)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.writes, [])

    def test_interrupted_chapter_creation_is_reconciled_by_extracted_hash(self):
        self.replace_cached_pdf('TD1', make_pdf(['one', 'two', 'three']))
        self.set_first_pages('1-2')
        self.fail_after_save = True
        with self.assertRaisesRegex(MoodleError, 'lost'):
            self.run_upload()
        self.assertEqual(next(iter(self.records().values()))['pending_sha256'], self.hashes[100])
        self.fail_after_save = False
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'create'])
        self.assertEqual(self.writes, [100, 101])

    def test_visibility_override_is_applied_on_creation(self):
        self.set_first_visible(False)
        reports = self.run_upload()
        self.assertEqual(self.visibilities, {100: 0, 101: 1})
        self.assertIs(reports[0]['visible'], False)
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])

    def test_visibility_changes_without_pdf_changes_then_skips_matching_rerun(self):
        self.run_upload()
        for visible in (False, True):
            with self.subTest(visible=visible):
                self.set_first_visible(visible)
                self.publisher.publish.reset_mock()
                reports = self.run_upload()
                self.assertEqual([r['action'] for r in reports], ['update', 'unchanged'])
                self.assertIs(reports[0]['visible'], visible)
                self.assertEqual(self.visibilities[100], int(visible))
                self.assertEqual(self.hashes[100], DIGEST)
                self.publisher.publish.assert_called_once()
                self.assertIs(self.publisher.publish.call_args.kwargs['replace_pdf'], False)
                self.publisher.publish.reset_mock()
                self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
                self.publisher.publish.assert_not_called()

    def test_manual_visibility_change_is_corrected_without_relying_on_saved_state(self):
        self.set_first_visible(True)
        self.run_upload()
        self.visibilities[100] = 0
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])
        self.assertEqual(self.visibilities[100], 1)

    def test_omitted_visibility_preserves_manual_setting_even_during_pdf_replacement(self):
        self.run_upload()
        self.visibilities[100] = 0
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
        self.run_upload(force=True)
        self.assertEqual(self.visibilities, {100: 0, 101: 1})
        self.client.resource_visibility.assert_not_called()
        self.assertTrue(all(call.kwargs['visible'] is None for call in self.publisher.publish.call_args_list))

    def test_removing_override_preserves_current_visibility(self):
        self.set_first_visible(False)
        self.run_upload()
        self.set_first_visible(None)
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
        self.assertEqual(self.visibilities[100], 0)

    def test_visibility_dry_run_reports_intent_without_changing_resource_or_state(self):
        self.run_upload()
        self.set_first_visible(False)
        before = self.config.sync.state_file.read_bytes()
        self.publisher.publish.reset_mock()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            reports = upload_configured(self.config, dry_run=True)
        self.assertEqual([r['action'] for r in reports], ['update', 'unchanged'])
        self.assertIn('Would update:', output.getvalue())
        self.assertIn('visibility: hidden', output.getvalue())
        self.assertEqual(self.visibilities[100], 1)
        self.assertEqual(self.config.sync.state_file.read_bytes(), before)
        self.publisher.publish.assert_not_called()

    def test_ignored_visibility_setting_is_not_recorded_as_success(self):
        self.run_upload()
        self.set_first_visible(False)

        def ignore_visibility(*args, **kwargs):
            self.publish(*args, **{**kwargs, 'visible': None})

        self.publisher.publish.side_effect = ignore_visibility
        with self.assertRaisesRegex(MoodleError, 'visibility verification failed'):
            self.run_upload()
        self.assertIn('pending_sha256', next(iter(self.records().values())))
        self.assertEqual(self.visibilities[100], 1)

    def test_interrupted_visibility_save_is_reconciled_without_another_write(self):
        self.run_upload()
        self.set_first_visible(False)
        self.fail_after_save = True
        with self.assertRaisesRegex(MoodleError, 'lost'):
            self.run_upload()
        self.assertEqual(self.visibilities[100], 0)
        self.assertIn('pending_sha256', next(iter(self.records().values())))
        self.fail_after_save = False
        self.publisher.publish.reset_mock()
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
        self.publisher.publish.assert_not_called()
        self.assertNotIn('pending_sha256', next(iter(self.records().values())))

    def test_section_id_reuses_publications_created_by_name_and_form_position(self):
        self.run_upload()
        original = self.config.sync.state_file.read_bytes()
        self.set_first_section(20182)
        reports = self.run_upload()
        self.assertEqual([r['action'] for r in reports], ['unchanged', 'unchanged'])
        self.assertEqual(reports[0]['section_name'], 'Test')
        self.assertEqual(reports[0]['section_id'], 20182)
        self.assertEqual(self.config.sync.state_file.read_bytes(), original)
        # Fake publish asserts that the form receives position 6, not ID 20182.
        self.run_upload(force=True)
        self.assertEqual(self.writes, [100, 101, 100, 101])
        self.assertEqual(len(self.records()), 2)

    def test_section_position_is_not_accepted_as_database_id(self):
        self.set_first_section(6)
        with self.assertRaisesRegex(MoodleError, 'Section ID 6 was not found'):
            self.run_upload()
        self.assertEqual(self.writes, [])

    def test_name_and_id_aliases_cannot_publish_twice_to_one_destination(self):
        first = self.config.files[0]
        duplicate = replace(first, target=replace(first.target, section=20182, name='Another name'))
        self.config = replace(self.config, files=(*self.config.files, duplicate))
        with self.assertRaisesRegex(ValueError, 'same Moodle destination'):
            self.run_upload()
        self.assertEqual(self.writes, [])

    def test_display_rename_preserves_id_filename_and_hash_and_then_skips(self):
        self.run_upload()
        old_keys = list(self.records())
        self.set_first_name('TD1')
        self.assertEqual([r['action'] for r in self.run_upload(dry_run=True)], ['update', 'unchanged'])
        self.assertEqual(self.modules[0]['name'], 'TD1.pdf')
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])
        self.assertEqual(self.modules[0], {'id': 100, 'name': 'TD1', 'modname': 'resource'})
        self.assertEqual(self.hashes[100], DIGEST)
        self.assertEqual(list(self.records()), old_keys)
        self.assertEqual(self.records()[old_keys[0]], {'cmid': 100, 'name': 'TD1', 'sha256': DIGEST})
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])
        self.set_first_name('TD1.pdf')
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])
        self.assertEqual(self.modules[0]['name'], 'TD1.pdf')
        self.assertEqual(self.writes, [100, 101, 100, 100])

    def test_custom_display_name_creation_then_reuse(self):
        self.set_first_name('TD1')
        self.run_upload()
        self.assertEqual(self.modules[0]['name'], 'TD1')
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'unchanged'])

    def test_display_rename_conflict_stops_before_writes(self):
        self.run_upload()
        self.set_first_name('TD1')
        self.modules.append({'id': 999, 'name': 'TD1', 'modname': 'resource'})
        with self.assertRaisesRegex(MoodleError, 'conflicts'):
            self.run_upload()
        self.assertEqual(self.writes, [100, 101])

    def test_duplicate_configured_display_names_stop_before_writes(self):
        self.set_first_name('TD2.pdf')
        with self.assertRaisesRegex(ValueError, 'same Moodle display name'):
            self.run_upload()
        self.assertEqual(self.writes, [])

    def test_interrupted_rename_recovers_even_after_another_yaml_name_change(self):
        self.run_upload()
        self.set_first_name('TD1')
        self.fail_after_save = True
        with self.assertRaisesRegex(MoodleError, 'lost'):
            self.run_upload()
        self.assertEqual(next(iter(self.records().values()))['pending_name'], 'TD1')
        self.set_first_name('Introduction')
        self.fail_after_save = False
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'unchanged'])
        self.assertEqual(self.modules[0]['name'], 'Introduction')
        self.assertEqual(self.writes, [100, 101, 100, 100])

    def test_dry_run_changes_neither_state_nor_course(self):
        previous = self.config.sync.state_file.read_bytes()
        self.assertEqual([r['action'] for r in self.run_upload(dry_run=True)], ['create', 'create'])
        self.assertEqual(self.config.sync.state_file.read_bytes(), previous)
        self.assertEqual(self.writes, [])

    def test_interrupted_create_is_recovered_without_duplicate(self):
        self.fail_after_save = True
        with self.assertRaisesRegex(MoodleError, 'lost'):
            self.run_upload()
        record = next(iter(self.records().values()))
        self.assertEqual(record, {'pending_sha256': DIGEST})
        self.fail_after_save = False
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'create'])
        self.assertEqual(self.writes, [100, 101])
        self.assertTrue(all('pending_sha256' not in r for r in self.records().values()))

    def test_partial_failure_keeps_first_success_and_retry_uploads_only_second(self):
        self.fail_file = 'TD2.pdf'
        with self.assertRaisesRegex(MoodleError, 'Draft failed'):
            self.run_upload()
        self.assertEqual(len(self.records()), 1)
        self.fail_file = None
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'create'])
        self.assertEqual(self.writes, [100, 101])

    def test_remote_edit_name_collision_or_duplicate_stops_before_any_writes(self):
        self.modules = [{'id': 9, 'name': 'TD2.pdf', 'modname': 'resource'}]
        self.hashes[9] = 'unrelated'
        with self.assertRaisesRegex(MoodleError, 'unrecognized'):
            self.run_upload()
        self.assertEqual(self.writes, [])
        self.modules *= 2
        with self.assertRaisesRegex(MoodleError, 'ambiguous'):
            self.run_upload()

    def test_renamed_managed_resource_is_not_recreated(self):
        self.run_upload()
        self.modules[0]['name'] = 'Renamed'
        with self.assertRaisesRegex(MoodleError, 'moved or renamed'):
            self.run_upload()
        self.assertEqual(self.writes, [100, 101])

    def test_remote_deletion_recreates_only_missing_resource(self):
        self.run_upload()
        self.modules.pop()
        self.assertEqual([r['action'] for r in self.run_upload()], ['unchanged', 'create'])

    def test_modified_cache_is_rejected_before_network(self):
        (self.config.sync.cache_dir / PROJECT / 'TD/TD1.pdf').write_bytes(PDF + b'bad')
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.run_upload()
        self.client.course.assert_not_called()
        self.assertEqual(self.records(), {})

    def test_bad_remote_verification_does_not_mark_success(self):
        self.publisher.pdf_hash.side_effect = lambda *_: 'wrong'
        with self.assertRaisesRegex(MoodleError, 'verification failed'):
            self.run_upload()
        self.assertEqual(next(iter(self.records().values())), {'pending_sha256': DIGEST})

    def test_update_returning_a_different_resource_id_is_not_success(self):
        self.run_upload()

        def changed_id(*args, **kwargs):
            self.publish(*args, **kwargs)
            self.modules[0]['id'] = 900
            self.hashes[900] = DIGEST

        self.publisher.publish.side_effect = changed_id
        with self.assertRaisesRegex(MoodleError, 'verification failed'):
            self.run_upload(force=True)
        first = next(iter(self.records().values()))
        self.assertEqual(first['cmid'], 100)
        self.assertEqual(first['pending_sha256'], DIGEST)

    def test_interrupted_create_can_be_recovered_after_new_local_build(self):
        self.fail_after_save = True
        with self.assertRaises(MoodleError):
            self.run_upload()
        self.fail_after_save = False
        changed = PDF + b'\nnext build'
        state = load_state(self.config.sync.state_file)
        (self.config.sync.cache_dir / PROJECT / 'TD/TD1.pdf').write_bytes(changed)
        state['projects']['https://plm.example/project/' + PROJECT]['builds']['TD/TD1.tex']['sha256'] = sha256(changed).hexdigest()
        save_state(self.config.sync.state_file, state)
        self.assertEqual([r['action'] for r in self.run_upload()], ['update', 'create'])
        self.assertEqual(self.writes, [100, 100, 101])

    def test_cli_sync_fetches_before_upload_and_dry_run_never_fetches(self):
        with patch('plm_moodle_sync.sync.fetch_configured') as fetch, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['sync', '--config', str(self.config.path)]), 0)
            fetch.assert_called_once()
            fetch.reset_mock()
            self.assertEqual(main(['upload', '--config', str(self.config.path), '--dry-run']), 0)
            fetch.assert_not_called()

    def test_two_yaml_uploads_use_distinct_projects_and_courses_with_shared_state(self):
        import yaml

        first = yaml.safe_load(self.config.path.read_text())
        first['files'] = first['files'][:1]
        self.config.path.write_text(yaml.safe_dump(first))
        second = deepcopy(first)
        other_project = 'f' * 24
        second['plmlatex']['project_id'] = other_project
        second['moodle']['course_id'] = 456
        other_path = self.root / 'other.yaml'
        other_path.write_text(yaml.safe_dump(second))
        other_pdf = PDF + b'\nfrom the second project'
        cache_path = self.config.sync.cache_dir / other_project / 'TD/TD1.pdf'
        cache_path.parent.mkdir(parents=True)
        cache_path.write_bytes(other_pdf)
        state = load_state(self.config.sync.state_file)
        state['projects']['https://plm.example/project/' + other_project] = {
            'builds': {'TD/TD1.tex': {'pdf': 'TD/TD1.pdf', 'sha256': sha256(other_pdf).hexdigest()}}}
        save_state(self.config.sync.state_file, state)

        modules = {301: [], 456: []}

        def course(course_id):
            return {'sections': [{'id': course_id + 1000, 'number': 6, 'name': 'Test',
                                  'modules': deepcopy(modules[course_id])}]}

        def publish(course_id, number, filename, content, *, before_submit, **kwargs):
            before_submit()
            cmid = course_id + 2000
            modules[course_id].append({'id': cmid, 'name': filename, 'modname': 'resource'})
            self.hashes[cmid] = sha256(content).hexdigest()

        self.client.course.side_effect = course
        self.publisher.publish.side_effect = publish
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['upload', '--config', str(self.config.path), str(other_path)]), 0)
            records = self.records()
            self.assertEqual(main(['upload', '--config', str(self.config.path), str(other_path)]), 0)
        self.assertEqual([call.args[0] for call in self.publisher.publish.call_args_list], [301, 456])
        self.assertEqual([call.args[3] for call in self.publisher.publish.call_args_list], [PDF, other_pdf])
        self.assertEqual(records, self.records())
        self.assertEqual({record['cmid'] for record in records.values()}, {2301, 2456})
        self.assertEqual(set(records), {SERVER + '/course/301/section/1301/file/TD1.pdf',
                                        SERVER + '/course/456/section/1456/file/TD1.pdf'})
        self.assertEqual(load_state(self.config.sync.state_file)['projects'], state['projects'])

    def test_overlapping_state_writers_are_rejected(self):
        with state_lock(self.config.sync.state_file):
            with self.assertRaisesRegex(StateError, 'Another sync'), state_lock(self.config.sync.state_file):
                self.fail('Acquired the same lock twice')
