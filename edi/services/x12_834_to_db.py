def convert_834_to_member(parsed_segments):

    member = {}

    for segment in parsed_segments:

        segment_name = segment["segment"]
        elements = segment["elements"]


        if segment_name == "NM1":

            # NM103 = Last Name
            if len(elements) > 2:
                member["last_name"] = elements[2]


            # NM104 = First Name
            if len(elements) > 3:
                member["first_name"] = elements[3]


            # NM105 = Middle Name
            if len(elements) > 4:
                member["middle_name"] = elements[4]


        elif segment_name == "DMG":

            # DMG02 = Date of Birth
            if len(elements) > 1:
                member["dob"] = elements[1]


            # DMG03 = Gender
            if len(elements) > 2:
                member["gender"] = elements[2]


    return member