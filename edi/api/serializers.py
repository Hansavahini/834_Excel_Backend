from rest_framework import serializers


class EDIFileUploadSerializer(serializers.Serializer):

    file = serializers.FileField()

    def validate_file(self, value):

        allowed_extensions = [
            ".834",
            ".x12",
            ".txt"
        ]

        file_name = value.name.lower()

        if not any(
            file_name.endswith(ext)
            for ext in allowed_extensions
        ):
            raise serializers.ValidationError(
                "Invalid file type. Upload only 834 EDI files."
            )

        return value
class MappingSerializer(serializers.Serializer):

    excel_column = serializers.CharField()

    segment = serializers.CharField()

    element = serializers.CharField()

    occurrence = serializers.IntegerField(
        default=1
    )