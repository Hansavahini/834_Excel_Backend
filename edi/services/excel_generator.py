from openpyxl import Workbook


def generate_excel(
        headers,
        rows,
        output_path
):

    workbook = Workbook()

    sheet = workbook.active

    sheet.title = "834 Conversion"


    # Create static headers

    for col_index, header in enumerate(
        headers,
        start=1
    ):

        sheet.cell(
            row=1,
            column=col_index
        ).value = header



    # Insert dynamic data rows

    for row_index, row_data in enumerate(
        rows,
        start=2
    ):

        for col_index, header in enumerate(
            headers,
            start=1
        ):

            sheet.cell(
                row=row_index,
                column=col_index
            ).value = row_data.get(
                header,
                ""
            )


    workbook.save(output_path)


    return output_path