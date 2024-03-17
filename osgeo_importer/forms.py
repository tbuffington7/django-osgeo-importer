import logging
import os
import shutil
import tempfile
from zipfile import is_zipfile, ZipFile

from django import forms
from django.conf import settings
from django.db.models import Sum

from osgeo_importer.importers import VALID_EXTENSIONS
from osgeo_importer.utils import mkdir_p, sizeof_fmt
from osgeo_importer.validators import valid_file

from .models import UploadFile, UploadedData
from .validators import validate_inspector_can_read, validate_shapefiles_have_all_parts

USER_UPLOAD_QUOTA = getattr(settings, 'USER_UPLOAD_QUOTA', None)

logger = logging.getLogger(__name__)


class UploadFileForm(forms.Form):
    file = forms.FileField(widget=forms.FileInput(attrs={'multiple': True}))

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super(UploadFileForm, self).__init__(*args, **kwargs)

    class Meta:
        model = UploadFile
        fields = ['file']

    def clean(self):
        cleaned_data = super(UploadFileForm, self).clean()
        outputdir = tempfile.mkdtemp()
        files = self.files.getlist('file')
        process_files = []

        for f in files:
            errors = valid_file(f)
            if errors:
                logger.warning(', '.join(errors))
                continue
            if is_zipfile(f):
                with ZipFile(f) as zip:
                    for zipname in zip.namelist():
                        zipext = os.path.splitext(zipname)[-1].lstrip('.').lower()
                        if zipext in VALID_EXTENSIONS:
                            process_files.append(zipname)
            else:
                process_files.append(f.name)

        if not validate_shapefiles_have_all_parts(process_files):
            self.add_error('file', 'Shapefiles must include .shp, .dbf, .shx, .prj')

        cleaned_files = []
        for f in files:
            if f.name in process_files:
                with open(os.path.join(outputdir, f.name), 'wb') as outfile:
                    for chunk in f.chunks():
                        outfile.write(chunk)
                cleaned_files.append(outfile)
            elif is_zipfile(f):
                with ZipFile(f) as zip:
                    for zipfile in zip.namelist():
                        if zipfile in process_files or ('gdb/' in VALID_EXTENSIONS and zipfile.endswith('.gdb')):
                            mkdir_p(os.path.join(outputdir, os.path.dirname(zipfile)))
                            with zip.open(zipfile) as zf, open(os.path.join(outputdir, zipfile), 'wb') as outfile:
                                shutil.copyfileobj(zf, outfile)
                                cleaned_files.append(outfile)

        inspected_files = []
        file_names = [os.path.basename(f.name) for f in cleaned_files]
        upload_size = 0

        for cleaned_file in cleaned_files:
            cleaned_file_path = os.path.join(outputdir, os.path.basename(cleaned_file.name))
            if validate_inspector_can_read(cleaned_file_path):
                add_file = True
                name, ext = os.path.splitext(os.path.basename(cleaned_file.name))
                upload_size += os.path.getsize(cleaned_file_path)

                if ext == '.xml':
                    if f'{name}.shp' in file_names or name in file_names:
                        add_file = False

                if add_file:
                    inspected_files.append(cleaned_file)
            else:
                logger.warning(f'Inspector could not read file {cleaned_file_path} or file is empty')

        cleaned_data['file'] = inspected_files
        cleaned_data['upload_size'] = upload_size
        if USER_UPLOAD_QUOTA is not None:
            user_filesize = UploadedData.objects.filter(user=self.request.user).aggregate(s=Sum('size'))['s'] or 0
            if user_filesize + upload_size > USER_UPLOAD_QUOTA:
                shutil.rmtree(outputdir)
                self.add_error('file', f'User Quota Exceeded. Quota: {sizeof_fmt(USER_UPLOAD_QUOTA)} Used: {sizeof_fmt(user_filesize)} Adding: {sizeof_fmt(upload_size)}')

        return cleaned_data
