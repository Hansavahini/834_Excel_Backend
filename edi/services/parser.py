class EDI834Parser:

    def __init__(self, file_path):
        self.file_path = file_path


    def read_file(self):

        with open(
            self.file_path,
            "r",
            encoding="utf-8-sig"
        ) as file:
            return file.read()


    def extract_elements(self, content):

        segments = content.split("~")

        extracted_data = []

        segment_count = {}


        for segment in segments:

            segment = segment.strip()

            if not segment:
                continue


            elements = segment.split("*")

            segment_name = elements[0]


            # Track repeated segment occurrence
            segment_count[segment_name] = (
                segment_count.get(segment_name, 0) + 1
            )


            occurrence = segment_count[segment_name]


            for index, value in enumerate(
                elements[1:],
                start=1
            ):

                extracted_data.append({

                    "segment": segment_name,

                    "element": f"{segment_name}{index:02}",

                    "value": value,

                    "occurrence": occurrence

                })


        return extracted_data