def extract_loops(parsed_data):

    loops = []

    current_loop = []

    loop_id = 1


    for item in parsed_data:

        if item["segment"] == "INS":

            if current_loop:

                loops.append(
                    {
                        "loop_id": loop_id,
                        "data": current_loop
                    }
                )

                loop_id += 1

                current_loop = []


        current_loop.append(item)


    if current_loop:

        loops.append(
            {
                "loop_id": loop_id,
                "data": current_loop
            }
        )


    return loops