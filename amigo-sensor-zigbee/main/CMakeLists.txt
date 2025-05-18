idf_component_register(SRCS "zcl_utility.c" "main.c"
                    INCLUDE_DIRS "."
                    REQUIRES esp-zigbee-lib nvs_flash
                    )
