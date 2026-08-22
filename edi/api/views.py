from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from django.core.files.storage import default_storage

from .serializers import EDIFileUploadSerializer
from .serializers import MappingSerializer

from edi.services.mapping_store import save_mapping

from edi.services.parser import EDI834Parser
from edi.services.loop_extractor import extract_loops
from edi.services.row_builder import build_excel_rows
from edi.services.excel_generator import generate_excel
from edi.services.file_service import get_file_path
class HealthCheckView(APIView):

    def get(self, request):

        return Response({
            "status": "healthy",
            "service": "834 EDI Converter"
        })


class EDIUploadView(APIView):

    def post(self, request):

        serializer = EDIFileUploadSerializer(
            data=request.data
        )

        if serializer.is_valid():

            uploaded_file = serializer.validated_data["file"]

            file_path = default_storage.save(
                f"uploads/{uploaded_file.name}",
                uploaded_file
            )

            return Response(
                {
                    "message": "834 file uploaded successfully",
                    "file_path": file_path,
                    "status": "UPLOADED"
                },
                status=status.HTTP_201_CREATED
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )
class MappingCreateView(APIView):

    def post(self, request):

        serializer = MappingSerializer(
            data=request.data
        )


        if serializer.is_valid():

            mapping = save_mapping(
                serializer.validated_data
            )


            return Response(
                {
                    "message":"Mapping saved",
                    "mapping":mapping
                },
                status=201
            )


        return Response(
            serializer.errors,
            status=400
        )
class Convert834View(APIView):

    def post(self, request):

        file_path = request.data["file_path"]

        headers = request.data["headers"]

        mappings = request.data["mappings"]


        # 1. Read EDI file

        parser = EDI834Parser(
            file_path
        )


        content = parser.read_file()


        # 2. Extract segments/elements

        parsed_data = parser.extract_elements(
            content
        )


        # 3. Split member loops

        loops = extract_loops(
            parsed_data
        )


        # 4. Apply mapping

        rows = build_excel_rows(
            loops,
            mappings
        )


        # 5. Generate Excel

        output_file = "converted_834.xlsx"


        generate_excel(
            headers,
            rows,
            output_file
        )


        return Response(
            {
                "message":"834 converted successfully",
                "file":output_file,
                "rows_generated":len(rows)
            }
        )