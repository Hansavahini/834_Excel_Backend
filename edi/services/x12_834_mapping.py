def apply_mapping(parsed_data, mappings):

    output_row = {}


    for mapping in mappings:

        column_name = mapping["excel_column"]

        target_segment = mapping["segment"]

        target_element = mapping["element"]


        for item in parsed_data:

            if (
                item["segment"] == target_segment
                and
                item["element"] == target_element
            ):

                output_row[column_name] = item["value"]

                break


    return output_row
def get_segments(parsed_data):

    segments = set()

    for item in parsed_data:
        segments.add(item["segment"])

    return list(segments)



def get_elements(parsed_data, segment_name):

    elements = set()

    for item in parsed_data:

        if item["segment"] == segment_name:
            elements.add(item["element"])

    return list(elements)
def apply_mapping(parsed_data, mappings):

    output = {}


    for mapping in mappings:

        column_name = mapping["excel_column"]

        target_segment = mapping["segment"]

        target_element = mapping["element"]

        target_occurrence = mapping.get(
            "occurrence",
            1
        )


        for item in parsed_data:

            if (
                item["segment"] == target_segment
                and
                item["element"] == target_element
                and
                item["occurrence"] == target_occurrence
            ):

                output[column_name] = item["value"]

                break


    return output