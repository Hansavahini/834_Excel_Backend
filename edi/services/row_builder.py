def build_excel_rows(
        loops,
        mappings
):

    rows = []


    for loop in loops:

        row = {}


        for mapping in mappings:

            for item in loop["data"]:

                if (
                    item["segment"] == mapping["segment"]
                    and
                    item["element"] == mapping["element"]
                ):

                    row[
                        mapping["excel_column"]
                    ] = item["value"]

                    break


        rows.append(row)


    return rows