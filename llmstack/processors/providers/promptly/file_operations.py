import base64
import logging
import os
import shutil
import tempfile
import uuid
from enum import Enum
from typing import Optional

import grpc
from asgiref.sync import async_to_sync
from django.conf import settings
from pydantic import Field, root_validator

from llmstack.apps.schemas import OutputTemplate
from llmstack.common.acars.proto import runner_pb2, runner_pb2_grpc
from llmstack.common.utils.utils import validate_parse_data_uri
from llmstack.processors.providers.api_processor_interface import (
    ApiProcessorInterface,
    ApiProcessorSchema,
)

logger = logging.getLogger(__name__)


def _mime_type_from_file_ext(ext):
    if ext == "txt":
        return "text/plain"
    elif ext == "html":
        return "text/html"
    elif ext == "css":
        return "text/css"
    elif ext == "js":
        return "application/javascript"
    elif ext == "json":
        return "application/json"
    elif ext == "xml":
        return "application/xml"
    elif ext == "csv":
        return "text/csv"
    elif ext == "tsv":
        return "text/tab-separated-values"
    elif ext == "md":
        return "text/markdown"
    else:
        return "application/octet-stream"


class ExportAsType(str, Enum):
    PDF = "pdf"

    def __str__(self):
        return self.value


class FileOperationsInput(ApiProcessorSchema):
    content: str = Field(
        default="",
        description="The contents of the file. Skip this field if you want to create an archive of the directory",
    )
    filename: Optional[str] = Field(
        description="The name of the file to create. If not provided, a random name will be generated",
    )
    directory: Optional[str] = Field(
        description="The directory to create the file in. If not provided, the file will be created in a temporary directory and path is returned",
    )
    archive: bool = Field(
        default=False,
        description="If true, an archive with the contents of the directory will be created",
    )
    mimetype: Optional[str] = Field(
        description="The mimetype of the file. If not provided, it will be inferred from the filename",
    )
    export_as: Optional[str] = Field(
        default=None,
        description="The format to export the file as. If not provided, the file will be created as a text file",
    )

    @root_validator
    def validate_input(cls, values):
        mimetype = values.get("mimetype")
        if not mimetype:
            filename = values.get("filename")
            if filename:
                file_extension = filename.split(".")[-1]
                mimetype = _mime_type_from_file_ext(file_extension)
                values["mimetype"] = mimetype
        return values


def create_data_uri(data, mime_type="text/plain", base64_encode=False, filename=None):
    # Encode data in Base64 if requested
    if base64_encode:
        data = base64.b64encode(data).decode("utf-8")

    # Build the Data URI
    data_uri = f"data:{mime_type}"
    if filename:
        data_uri += f";name={filename}"
    if base64_encode:
        data_uri += ";base64"
    data_uri += f",{data}"

    return data_uri


class FileOperationsOutput(ApiProcessorSchema):
    directory: str = Field(description="The directory the file was created in")
    filename: str = Field(description="The name of the file created")
    objref: Optional[str] = Field(default=None, description="Object ref of the file created")
    archive: bool = Field(
        default=False,
        description="If true, then we just created an archive with contents from directory",
    )
    text: str = Field(
        default="",
        description="Textual description of the output",
    )


class FileOperationsConfiguration(ApiProcessorSchema):
    pass


def _create_archive(files, directory=""):
    """
    Using django storage, recursively copies all the files to a temporary directory and creates an archive
    """
    zip_file_bytes = None
    zip_filedata_uri = None

    # Create a temporary directory to store the files
    with tempfile.TemporaryDirectory() as temp_archive_dir:
        archive_name = f"{temp_archive_dir}.zip".replace("/", "_")

        # Create files in the temporary directory
        for file in files:
            name = file["name"]
            if directory and not name.startswith(directory):
                continue

            if os.path.dirname(name):
                abs_directory_path = os.path.join(temp_archive_dir, os.path.dirname(name))
                if not os.path.exists(abs_directory_path):
                    os.makedirs(abs_directory_path, exist_ok=True)

            data_uri = file["data_uri"]
            mime_type, file_name, b64_file_data = validate_parse_data_uri(data_uri)
            file_data_bytes = base64.b64decode(b64_file_data)

            with open(os.path.join(temp_archive_dir, name), "wb") as f:
                f.write(file_data_bytes)

        # Create an archive of the temporary directory
        shutil.make_archive(temp_archive_dir, "zip", temp_archive_dir)

        # Save the archive to the storage

        with open(f"{temp_archive_dir}.zip", "rb") as f:
            zip_file_bytes = f.read()
            zip_filedata_uri = create_data_uri(
                zip_file_bytes, "application/zip", base64_encode=True, filename=archive_name
            )

    return (zip_filedata_uri, archive_name)


class FileOperationsProcessor(
    ApiProcessorInterface[FileOperationsInput, FileOperationsOutput, FileOperationsConfiguration],
):
    @staticmethod
    def name() -> str:
        return "File Operations"

    @staticmethod
    def slug() -> str:
        return "file_operations"

    @staticmethod
    def description() -> str:
        return "Creates files, directories and archives with provided content"

    @staticmethod
    def provider_slug() -> str:
        return "promptly"

    @staticmethod
    def tool_only() -> bool:
        return True

    @classmethod
    def get_output_template(cls) -> Optional[OutputTemplate]:
        return OutputTemplate(markdown="File: {{objref}}")

    def process(self) -> dict:
        output_stream = self._output_stream

        content = self._input.content
        filename = self._input.filename or str(uuid.uuid4())
        directory = self._input.directory or ""
        archive = self._input.archive

        # Create an archive if directory is provided but not content with
        # archive flag set
        if not content and archive:
            result = self._get_all_session_assets(include_name=True, include_data=True)
            if result and "assets" in result and len(result["assets"]):
                zipped_assets, archive_name = _create_archive(result["assets"], directory)
                asset = self._upload_asset_from_url(asset=zipped_assets)
                async_to_sync(output_stream.write)(
                    FileOperationsOutput(
                        directory="",
                        filename=archive_name,
                        objref=asset,
                        archive=True,
                        text="Archive created with contents from directory",
                    ),
                )
            else:
                async_to_sync(output_stream.write)(
                    FileOperationsOutput(
                        directory=directory,
                        objref=None,
                        filename=filename,
                        archive=True,
                        text="No files found to create an archive",
                    ),
                )
        elif content and not archive:
            data_uri = None
            if self._input.export_as:
                if self._input.export_as == ExportAsType.PDF:
                    channel = grpc.insecure_channel(f"{settings.RUNNER_HOST}:{settings.RUNNER_PORT}")

                    stub = runner_pb2_grpc.RunnerStub(channel)

                    request = runner_pb2.WordProcessorRequest(
                        create=runner_pb2.WordProcessorFileCreate(
                            filename=self._input.filename or f"{str(uuid.uuid4())}.pdf",
                            mime_type=runner_pb2.ContentMimeType.PDF,
                            html=self._input.content,
                        )
                    )
                    response_iter = stub.GetWordProcessor(
                        iter([request]),
                    )
                    for response in response_iter:
                        data = response.files[0].data
                        data_uri = create_data_uri(
                            data, "application/pdf", base64_encode=True, filename=request.create.filename
                        )
                else:
                    raise ValueError(f"Unsupported export_as type: {self._config.export_as}")
            else:
                full_file_path = f"{directory}/{filename}" if directory else filename
                # Create a dataURI for the file
                data_uri = create_data_uri(
                    self._input.content.encode("utf-8"),
                    self._input.mimetype,
                    base64_encode=True,
                    filename=full_file_path,
                )

            if data_uri:
                asset = self._upload_asset_from_url(asset=data_uri)

                async_to_sync(output_stream.write)(
                    FileOperationsOutput(
                        directory=directory,
                        filename=filename,
                        objref=asset,
                        archive=False,
                        text=content,
                    ),
                )
            else:
                raise ValueError("Failed to create data uri")

        # Finalize the output stream
        output = output_stream.finalize()
        return output
