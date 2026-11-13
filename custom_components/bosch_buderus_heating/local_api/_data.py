"""Reviewed API facts. Rebuild with scripts/update_local_api_catalog.py.

Adapted from Bosch Thermotechnik GmbH (2026), Apache-2.0.
See LICENSE in this directory. Descriptions and examples are omitted.
"""

from typing import Any

SOURCE_COMMIT = "78e36b3b57e11ac6266558acba96c13bee88ceff"
SOURCE_SHA256 = "ee7f17d9b783e4722b82e9c141dd63f1bc717c13c04bb435bfb2a7989323fb93"

SCHEMAS: dict[str, Any] = {
    "BaseResource": {
        "type": "object",
        "required": ["id", "type", "writeable"],
        "properties": {
            "id": {"type": "string"},
            "type": {"type": "string"},
            "writeable": {"type": "integer", "enum": [0, 1]},
            "recordable": {"type": "integer", "enum": [0, 1]},
        },
    },
    "StringValue": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "type": {"enum": ["stringValue"]},
                    "value": {"type": "string"},
                    "allowedValues": {"type": "array", "items": {"type": "string"}},
                },
            },
        ]
    },
    "ReadableResource": {
        "type": "object",
        "properties": {"writeable": {"type": "integer", "enum": [0]}},
    },
    "FloatValue": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "type": {"enum": ["floatValue"]},
                    "value": {"type": "number", "format": "float"},
                    "unitOfMeasure": {"type": "string"},
                    "minValue": {"type": "number", "format": "float"},
                    "maxValue": {"type": "number", "format": "float"},
                    "state": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": {"type": "number"},
                        },
                    },
                    "stepSize": {"type": "number", "format": "float"},
                    "valueConfig": {
                        "type": "array",
                        "items": {
                            "required": ["referenceSensor"],
                            "properties": {"referenceSensor": {"type": "string"}},
                        },
                    },
                },
            },
        ]
    },
    "WriteableResource": {
        "type": "object",
        "properties": {"writeable": {"type": "integer", "enum": [1]}},
    },
    "InclusionWhitelist": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "type": {"enum": ["inclusionWhitelist"]},
                    "value": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "assignedHC",
                                "actualDevices",
                                "maxSupportedDevices",
                                "sgtinPattern",
                            ],
                            "properties": {
                                "assignedHC": {"type": "string"},
                                "actualDevices": {"type": "integer"},
                                "maxSupportedDevices": {"type": "integer"},
                                "sgtinPattern": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["type", "pattern"],
                                        "properties": {
                                            "type": {"type": "string"},
                                            "pattern": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        ]
    },
    "StringConfig": {
        "type": "object",
        "required": ["charset", "codingType", "maxLength"],
        "properties": {
            "charset": {"type": "string"},
            "codingType": {"type": "string"},
            "maxLength": {"type": "integer"},
        },
    },
    "BinaryStringValue": {
        "allOf": [
            {"$ref": "#/components/schemas/StringValue"},
            {
                "type": "object",
                "properties": {"value": {"type": "string", "enum": ["on", "off"]}},
            },
        ]
    },
    "CustomNameStringValue": {
        "allOf": [
            {"$ref": "#/components/schemas/StringValue"},
            {
                "type": "object",
                "required": ["valueConfig"],
                "properties": {
                    "value": {"type": "string"},
                    "valueConfig": {"$ref": "#/components/schemas/StringConfig"},
                },
            },
        ]
    },
    "Reference": {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "uri"],
        "properties": {
            "id": {"type": "string"},
            "uri": {"type": "string", "format": "uri"},
        },
    },
    "IntegerValue": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "type": {"enum": ["integerValue"]},
                    "value": {"type": "integer"},
                    "unitOfMeasure": {"type": "string"},
                    "minValue": {"type": "integer"},
                    "maxValue": {"type": "integer"},
                    "stepSize": {"type": "integer"},
                    "state": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": {"type": "integer"},
                        },
                    },
                },
            },
        ]
    },
    "TimezoneStringValue": {
        "allOf": [
            {"$ref": "#/components/schemas/StringValue"},
            {"type": "object", "properties": {"value": {"type": "string"}}},
        ]
    },
    "UpdateStatus": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "type": {"enum": ["updateStatus"]},
                    "value": {
                        "properties": {
                            "recipeId": {"type": "string"},
                            "reciepeId": {"type": "string"},
                            "updateId": {"type": "string"},
                            "status": {
                                "type": "object",
                                "properties": {
                                    "value": {
                                        "type": "string",
                                        "enum": [
                                            "idle",
                                            "start",
                                            "run",
                                            "download",
                                            "in_progress",
                                            "success",
                                            "failure",
                                            "done",
                                        ],
                                    },
                                    "allowedValues": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                },
                            },
                            "progress": {
                                "type": "object",
                                "properties": {
                                    "nsteps": {"type": "number", "minimum": 0},
                                    "cur_step": {"type": "number", "minimum": 0},
                                    "cur_percent": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "percent": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "cur_stepName": {"type": "string"},
                                    "cur_stepType": {
                                        "type": "object",
                                        "properties": {
                                            "value": {"type": "string"},
                                            "allowedValues": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                        },
                                    },
                                },
                            },
                        }
                    },
                },
            },
        ]
    },
    "UpdateStatusStringValue": {
        "allOf": [
            {"$ref": "#/components/schemas/StringValue"},
            {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "enum": ["inprogress", "completed", "interrupted"],
                    }
                },
            },
        ]
    },
    "UpdateReport": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["timeStamp", "recipeId", "status", "gatewayId", "values"],
                "properties": {
                    "type": {"enum": ["updateReport"]},
                    "timeStamp": {"type": "string"},
                    "recipeId": {"type": "string"},
                    "status": {"type": "string", "enum": ["", "SUCCESS", "ERROR"]},
                    "gatewayId": {"type": "string"},
                    "logs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["msg", "timeStampMs"],
                            "properties": {
                                "msg": {"type": "string"},
                                "timeStampMs": {"type": "string"},
                            },
                        },
                    },
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["moduleId", "status", "timeStamp"],
                            "properties": {
                                "moduleId": {
                                    "oneOf": [{"type": "string"}, {"type": "number"}]
                                },
                                "distributionSetId": {"type": "string"},
                                "status": {
                                    "type": "string",
                                    "enum": ["SUCCESS", "ERROR", "CANCELLED"],
                                },
                                "previousSwVersion": {"type": "string"},
                                "statusCode": {"type": "number", "minimum": 0},
                                "timeStamp": {"type": "string"},
                                "logs": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "msg": {"type": "string"},
                                            "timeStampMs": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        ]
    },
    "ApList": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "type": {"enum": ["apList"]},
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["ssid", "strength", "secType"],
                            "properties": {
                                "ssid": {"type": "string"},
                                "strength": {"type": "string"},
                                "secType": {"type": "string"},
                            },
                        },
                    },
                },
            },
        ]
    },
    "WifiPairingStatus": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "type": {"enum": ["stringValue"]},
                    "value": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                    "allowedValues": {
                        "type": "array",
                        "items": {"oneOf": [{"type": "boolean"}, {"type": "string"}]},
                    },
                },
            },
        ]
    },
    "ArrayData": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "type": {"enum": ["arrayData"]},
                    "values": {"type": "array", "items": {"type": "string"}},
                    "allowedValues": {"type": "array", "items": {"type": "string"}},
                },
            },
        ]
    },
    "EmonValue": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "type": {"enum": ["emonValue"]},
                    "unit": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "maxProperties": 1,
                            "properties": {
                                "outputProduced": {"type": "number"},
                                "eheater": {"type": "number"},
                                "compressor": {"type": "number"},
                                "solar": {"type": "number"},
                                "burner": {"type": "number"},
                                "electricity": {"type": "number"},
                                "ventilation": {"type": "number"},
                                "ventilationHeatRecovered": {"type": "number"},
                                "ch": {"type": "number"},
                                "dhw": {"type": "number"},
                                "pool": {"type": "number"},
                                "cooling": {"type": "number"},
                                "total": {"type": "number"},
                            },
                        },
                    },
                },
            },
        ]
    },
    "ConfigData": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "type": {"enum": ["configData"]},
                    "values": {"type": "object"},
                },
            },
        ]
    },
    "ErrorList": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "type": {"enum": ["errorList"]},
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["dcd", "ccd", "orig", "dlv", "fc"],
                            "properties": {
                                "dcd": {"type": "string"},
                                "ccd": {"type": "number"},
                                "orig": {"type": "string"},
                                "dlv": {"type": "string"},
                                "fc": {"type": "string"},
                            },
                        },
                    },
                },
            },
        ]
    },
    "RecordingSampleRate": {"type": "string", "enum": ["PT1H", "P1D", "P1M", "all"]},
    "RecordingStartDate": {"type": "string", "format": "date"},
    "RecordingEndDate": {"type": "string", "format": "date"},
    "yRecordingExtended": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["recordedResource", "recordingType", "values"],
                "properties": {
                    "type": {"enum": ["yRecordingExtended"]},
                    "unitOfMeasure": {"type": "string"},
                    "recordedResource": {
                        "type": "object",
                        "required": ["id", "uri"],
                        "properties": {
                            "id": {"type": "string"},
                            "uri": {"type": "string", "format": "uri"},
                        },
                    },
                    "recordingType": {"type": "string", "enum": ["actual"]},
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "sampleRate",
                                "startTime",
                                "endTime",
                                "recording",
                            ],
                            "properties": {
                                "sampleRate": {
                                    "type": "string",
                                    "enum": ["PT1H", "P1D", "P1M"],
                                },
                                "startTime": {"type": "string", "format": "date-time"},
                                "endTime": {"type": "string", "format": "date-time"},
                                "recording": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["y", "c"],
                                        "properties": {
                                            "y": {"type": "number"},
                                            "c": {"type": "number"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        ]
    },
    "RefEnum": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["references"],
                "properties": {
                    "type": {"enum": ["refEnum"]},
                    "references": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Reference"},
                    },
                },
            },
        ]
    },
    "SystemBasicInfo": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["status", "timeStamp", "gatewayId", "values"],
                "properties": {
                    "type": {"enum": ["systeminfo"]},
                    "status": {"type": "string"},
                    "timeStamp": {"type": "string", "pattern": "^(?:\\d{10})?$"},
                    "gatewayId": {"type": "string"},
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "Tok",
                                "Id",
                                "Ver",
                                "ModuleSerialNumber",
                                "ModuleHwIdentStr",
                            ],
                            "properties": {
                                "Tok": {"type": "string"},
                                "Id": {"type": "string"},
                                "Ver": {"type": "string"},
                                "ModuleSerialNumber": {
                                    "oneOf": [{"type": "string"}, {"type": "number"}]
                                },
                                "ProductSerialNumber": {"type": "string"},
                                "ModuleHwIdentStr": {"type": "string"},
                                "ProductName": {"type": "string"},
                            },
                        },
                    },
                },
            },
        ]
    },
    "DeviceErrors": {
        "allOf": [
            {"$ref": "#/components/schemas/BaseResource"},
            {
                "type": "object",
                "required": ["values"],
                "properties": {
                    "type": {"enum": ["deviceErrors"]},
                    "values": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "errorType",
                                "occurrenceId",
                                "value",
                                "allowedValues",
                            ],
                            "properties": {
                                "errorType": {"type": "string"},
                                "occurrenceId": {"type": "number"},
                                "value": {"type": "string"},
                                "allowedValues": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
        ]
    },
    "VentilationExhaustFanLevelValues": {
        "allOf": [
            {"$ref": "#/components/schemas/StringValue"},
            {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "enum": ["off", "min", "red", "nom", "max", "dem"],
                    },
                    "allowedValues": {"type": "array", "items": {"type": "string"}},
                },
            },
        ]
    },
}

OPERATIONS: dict[str, Any] = {
    "/devices/inclusionTime": {
        "group": "devices",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/devices/inclusionWhitelist": {
        "group": "devices",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/InclusionWhitelist"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/rhc/assignedTo": {
        "group": "devices",
        "parameters": [],
        "response": {"$ref": "#/components/schemas/StringValue"},
    },
    "/devices/uhc/assignedTo": {
        "group": "devices",
        "parameters": [],
        "response": {"$ref": "#/components/schemas/StringValue"},
    },
    "/gateway/brand": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/dataProcessing/status": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "enum": ["in_progress", "completed"],
                        }
                    },
                },
            ]
        },
    },
    "/gateway/eth/ip/ipv4": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/eth/mac": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/tzInfo/timeZone": {
        "group": "gateway",
        "parameters": [],
        "response": {"$ref": "#/components/schemas/TimezoneStringValue"},
    },
    "/gateway/update/status": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "oneOf": [
                {
                    "allOf": [
                        {"$ref": "#/components/schemas/BaseResource"},
                        {"$ref": "#/components/schemas/UpdateStatus"},
                    ]
                },
                {
                    "allOf": [
                        {"$ref": "#/components/schemas/BaseResource"},
                        {"$ref": "#/components/schemas/UpdateStatusStringValue"},
                    ]
                },
            ]
        },
    },
    "/gateway/update/report": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BaseResource"},
                {"$ref": "#/components/schemas/UpdateReport"},
            ]
        },
    },
    "/gateway/versionFirmware": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/versionHardware": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/wifi/apList": {
        "group": "gateway",
        "parameters": [
            {"name": "refresh", "in": "query", "schema": {"type": "boolean"}}
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ApList"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/wifi/hotspotRemainingTime": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/wifi/ip/ipv4": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/wifi/mac": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/wifi/pairingStatus": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/WifiPairingStatus"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/gateway/wifi/ssid": {
        "group": "gateway",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ApList"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/actualHeatDemand": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ArrayData"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/actualModulation": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/actualSupplyTemperature": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/compressor/powerElecActual": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/currentSupplySetpoint": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/dhw/cylinderTemperature": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/additionalHeater/flowTemp": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/additionalHeater/primary/status": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/eHeater/powerElecActual": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/eHeater/powerElecActualLowResolution": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/emon/chConsumption": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/emon/coolingConsumption": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/emon/dhwConsumption": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/emon/poolConsumption": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/emon/totalConsumption": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/emStatus": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/externalInputs/input1/state": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/externalInputs/input2/state": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/externalInputs/input3/state": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/externalInputs/input4/state": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/externalInputs/status/coolingBlock": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/fallbackOperation/status": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/flameStatus": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/numberOfRefrigerantCircuitsOnline": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/hybrid/activeHeatSource": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/numberOfStarts": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/pvContactState": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/returnTemperature": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/sensors/evaporatorTemp_tl1": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/sensors/evaporatorTemp_tl2": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/smartFunction/active": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ReadableResource"},
                {"$ref": "#/components/schemas/BinaryStringValue"},
            ]
        },
    },
    "/heatSources/Source/eHeater/status": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/systemPressure": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/workingTime/totalSystem": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/dhw/cylinderUpperTemp": {
        "group": "heat sources",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/notifications": {
        "group": "notifications",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ErrorList"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/pool/currentTemp": {
        "group": "pool",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/recordings/heatSources/actualSupplyTemperature": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ch/burner": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ch/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ch/eheater": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ch/electricity": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ch/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ch/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ch/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/cooling/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/cooling/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/cooling/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/cooling/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/dhw/burner": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/dhw/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/dhw/eheater": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/dhw/electricity": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/dhw/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/dhw/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/dhw/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/pool/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/pool/eheater": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/pool/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/pool/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/pool/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/burner": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/eheater": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/electricity": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/solar": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/total/ventilation": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/emon/ventilation/heatRecovered": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/system/sensors/temperatures/outdoor_t1": {
        "group": "recordings",
        "parameters": [
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/signals": {
        "group": "signals",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/RefEnum"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/signals/{signalId}": {
        "group": "signals",
        "parameters": [
            {
                "name": "signalId",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        "response": {
            "oneOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/StringValue"},
            ]
        },
    },
    "/system/basicInfo": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/SystemBasicInfo"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/bus": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/type": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "enum": [
                                "unknown",
                                "heatpump_cascade",
                                "heatpump_single",
                                "eHybrid",
                                "hybman",
                                "hybridBoiler",
                                "boiler",
                            ]
                        }
                    },
                },
            ]
        },
    },
    "/system/iSRC/installationStatus": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/iSRC/supportStatus": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/additionalHeaterCh": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/additionalHeaterDhwTank": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/additionalHeaterPrimaryCircuit": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/ch": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/cooling": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/dhw": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/pool": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/affectedDomain/refrigerantCircuit": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/dhwReduceTempOnAlarm/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/externalInputs/additionalHeaterBlock/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/externalInputs/appliancePowerBlock/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/externalInputs/appliancePowerLimit/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/externalInputs/coolingBlock/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/externalInputs/dhwBlock/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/externalInputs/heatingBlock/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/externalInputs/pv/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/highSourceTempStopCooling/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/highSourceTempStopHeating/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/lowSourceTempStopCooling/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/lowSourceTempStopHeating/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/maintenanceMode/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/powerElecDesired/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/powerElecLimitation/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/powerGuard/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/silentMode/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerConstraints/userAdditionalHeaterBlock/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerGuard/active": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/powerLimitation/active": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/sensors/temperatures/outdoor_t1": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/update/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BaseResource"},
                {"$ref": "#/components/schemas/UpdateStatus"},
            ]
        },
    },
    "/system/update/report": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BaseResource"},
                {"$ref": "#/components/schemas/UpdateReport"},
            ]
        },
    },
    "/system/variableTariff/ch/currentSetpoint": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/variableTariff/ch/highPriceDelta": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/system/variableTariff/ch/lowPriceDelta": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/system/variableTariff/ch/midPriceSetpoint": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/system/variableTariff/ch/optimization": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
                {"type": "object", "properties": {"value": {"enum": ["off", "on"]}}},
            ]
        },
    },
    "/system/variableTariff/ch/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/variableTariff/currentPriceCatagorization": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/variableTariff/dhw/currentOpmode": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {"value": {"enum": ["low", "high", "eco"]}},
                },
            ]
        },
    },
    "/system/variableTariff/dhw/highPriceEnable": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
                {"type": "object", "properties": {"value": {"enum": ["yes", "no"]}}},
            ]
        },
    },
    "/system/variableTariff/dhw/lowPriceEnable": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
                {"type": "object", "properties": {"value": {"enum": ["yes", "no"]}}},
            ]
        },
    },
    "/system/variableTariff/dhw/optimization": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
                {"type": "object", "properties": {"value": {"enum": ["off", "on"]}}},
            ]
        },
    },
    "/system/variableTariff/dhw/status": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/variableTariff/supportStatus": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ArrayData"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/system/variableTariff/tariffId": {
        "group": "system",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/zones/configuration": {
        "group": "zones",
        "parameters": [],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ConfigData"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/actualHumidity": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/actuatorModulation": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/actuatorPower": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/assignedHC": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/battery": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/currentRoomSetpoint": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/errors": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/DeviceErrors"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/name": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/operationMode": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "enum": ["auto", "manual", "party"]}
                    },
                },
            ]
        },
    },
    "/devices/{deviceId}/rfConnectionStatus": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string", "enum": ["OFFLINE", "ONLINE"]}
                    },
                },
            ]
        },
    },
    "/devices/{deviceId}/rfErrorCause": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "enum": [
                                "INVALID_CONFIGURATION",
                                "MAX_DEVICES_REACHED",
                                "NO_ERROR",
                                "PAIRING_TIMEOUT",
                            ],
                        }
                    },
                },
            ]
        },
    },
    "/devices/{deviceId}/rfPairedStatus": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "type": "string",
                            "enum": ["NOT_PAIRED", "IN_PAIRING", "PAIRED", "EXCLUDED"],
                        }
                    },
                },
            ]
        },
    },
    "/devices/{deviceId}/rfTimeofConnectionLost": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/roomtemperature": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/sgtin": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/signal": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/signalIcon": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/type": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/versionFirmware": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/devices/{deviceId}/zoneId": {
        "group": "devices",
        "parameters": [
            {
                "name": "deviceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "device1",
                        "device2",
                        "device3",
                        "device4",
                        "device5",
                        "device6",
                        "device7",
                        "device8",
                        "device9",
                        "device10",
                        "device11",
                        "device12",
                        "device13",
                        "device14",
                        "device15",
                        "device16",
                        "device17",
                        "device18",
                        "device19",
                        "device20",
                        "device21",
                        "device22",
                        "device23",
                        "device24",
                        "device25",
                        "device26",
                        "device27",
                        "device28",
                        "device29",
                        "device30",
                        "device31",
                        "device32",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/currentFriwaSupplyTemperature": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/friwaPrimaryPumpModulation": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/actualTemp": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/chargeRemainingTime": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/currentSetpoint": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/currentTemperatureLevel": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/outTemp": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/overallStatus": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "enum": [
                                "dhw_enabled",
                                "dhw_disabled",
                                "floor_drying",
                                "td",
                                "extra",
                                "away",
                                "holiday",
                                "manual_on_high",
                                "manual_on_low",
                                "manual_on_eco",
                                "manual_off",
                                "auto",
                            ]
                        }
                    },
                },
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/tdrunningStatus": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/BinaryStringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/dhwCircuits/{dhwCircuitId}/volumeFlow": {
        "group": "dhw circuits",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/actualHumidity": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/boostRemainingTime": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/currentRoomSetpoint": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/currentSuWiMode": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {"value": {"enum": ["forced", "off", "cooling"]}},
                },
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/maxFlowTemp": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/mixerPosition": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/openWindowDetection/status": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {"value": {"enum": ["closed", "opened"]}},
                },
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/overallStatus": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
                {
                    "type": "object",
                    "properties": {
                        "value": {
                            "enum": [
                                "ch_enabled",
                                "ch_disabled",
                                "emergency_mode",
                                "floor_drying",
                                "summer_idle",
                                "boost",
                                "away",
                                "holiday",
                                "cooling_manual_on",
                                "cooling_manual_off",
                                "heating_manual_on",
                                "heating_manual_off",
                                "heating_auto",
                            ]
                        }
                    },
                },
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/pumpStatus": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatingCircuits/{heatingCircuitId}/roomtemperature": {
        "group": "heating circuits",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/numberOfStarts": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/oduFanSpeed": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/pumpVolumeFlow": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/status": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/compressorElecPowerActual": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/compressorTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/heatCarrierPumpSpeed": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/liquidPipeCoolingTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/liquidPipeHeatingTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/lowPressureTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/highPressureTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/hotGasTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/suctionGasTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/returnFlowTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/refrigerant/compressorActualSpeed": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/supplyFlowCondenserTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/workingTime": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/safetyBoard/status": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/brineCircuit/collectorInflowTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/brineCircuit/collectorOutflowTemp": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/brineCircuit/pressure": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/brineCircuit/pumpSpeed": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/brineCircuit/volumeFlow": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/IntegerValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/heatPumpType": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/emon/chConsumption": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/emon/dhwConsumption": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/heatSources/{heatSourceId}/emon/totalConsumption": {
        "group": "heat sources",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs1", "hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/EmonValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/recordings/dhwCircuits/{dhwCircuitId}/actualTemp": {
        "group": "recordings",
        "parameters": [
            {
                "name": "dhwCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["dhw1", "dhw2"]},
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatingCircuits/{heatingCircuitId}/roomtemperature": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatingCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["hc1", "hc2", "hc3", "hc4"]},
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/ch/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/ch/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/ch/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/ch/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/dhw/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/dhw/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/dhw/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/dhw/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/total/compressor": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/total/numberOfStarts": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/total/outputProduced": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/recordings/heatSources/{heatSourceId}/emon/total/workingTime": {
        "group": "recordings",
        "parameters": [
            {
                "name": "heatSourceId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": ["hs2", "hs3", "hs4", "hs5", "hs6"],
                },
            },
            {
                "name": "sampleRate",
                "in": "query",
                "required": True,
                "schema": {"$ref": "#/components/schemas/RecordingSampleRate"},
            },
            {
                "name": "startDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingStartDate"},
            },
            {
                "name": "endDate",
                "in": "query",
                "required": False,
                "schema": {"$ref": "#/components/schemas/RecordingEndDate"},
            },
        ],
        "response": {"$ref": "#/components/schemas/yRecordingExtended"},
    },
    "/solarCircuits/{solarCircuitId}/collectorTemperature": {
        "group": "solar circuits",
        "parameters": [
            {
                "name": "solarCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["sc1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/solarCircuits/{solarCircuitId}/dhwTankBottomTemperature": {
        "group": "solar circuits",
        "parameters": [
            {
                "name": "solarCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["sc1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/solarCircuits/{solarCircuitId}/pumpModulation": {
        "group": "solar circuits",
        "parameters": [
            {
                "name": "solarCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["sc1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/solarCircuits/{solarCircuitId}/solarYield": {
        "group": "solar circuits",
        "parameters": [
            {
                "name": "solarCircuitId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["sc1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/applianceRunTime": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/elAuxHeaterPower": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/electricalAuxHeaterPower": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/exhaustFanLevel": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/ReadableResource"},
                {"$ref": "#/components/schemas/VentilationExhaustFanLevelValues"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/exhaustFanPower": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/exhaustFanSpeed": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/filter/remainingTime": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/maxIndoorAirQuality": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/maxRelativeHumidity": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/exhaustTemp": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/externalAirQuality": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/externalHumidity": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/extractTemp": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/internalAirQuality": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/internalHumidity": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/outdoorTemp": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/supplyFanRotation": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/sensors/supplyTemp": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/summerBypass/flapPower": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/ventilation/{ventilationZoneId}/supplyFanPower": {
        "group": "ventilation",
        "parameters": [
            {
                "name": "ventilationZoneId",
                "in": "path",
                "required": True,
                "schema": {"type": "string", "enum": ["zone1"]},
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/zones/{zoneId}/averageActualHumidity": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/zones/{zoneId}/averageCurrentTemperature": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/zones/{zoneId}/childLock": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
                {
                    "type": "object",
                    "properties": {"value": {"enum": ["inactive", "active"]}},
                },
            ]
        },
    },
    "/zones/{zoneId}/cooling/manualRoomSetpoint": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/zones/{zoneId}/cooling/temporaryRoomSetpoint": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/zones/{zoneId}/cooling/operationMode": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
                {
                    "type": "object",
                    "properties": {"value": {"enum": ["auto", "manual", "off"]}},
                },
            ]
        },
    },
    "/zones/{zoneId}/currentRoomSetpoint": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/ReadableResource"},
            ]
        },
    },
    "/zones/{zoneId}/heating/manualRoomSetpoint": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/zones/{zoneId}/heating/operationMode": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
                {
                    "type": "object",
                    "properties": {"value": {"enum": ["auto", "manual", "off"]}},
                },
            ]
        },
    },
    "/zones/{zoneId}/heating/temporaryRoomSetpoint": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/FloatValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/zones/{zoneId}/icon": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/StringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
    "/zones/{zoneId}/name": {
        "group": "zones",
        "parameters": [
            {
                "name": "zoneId",
                "in": "path",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "zone1",
                        "zone2",
                        "zone3",
                        "zone4",
                        "zone5",
                        "zone6",
                        "zone7",
                        "zone8",
                        "zone9",
                        "zone10",
                        "zone11",
                        "zone12",
                        "zone13",
                        "zone14",
                        "zone15",
                        "zone16",
                    ],
                },
            }
        ],
        "response": {
            "allOf": [
                {"$ref": "#/components/schemas/CustomNameStringValue"},
                {"$ref": "#/components/schemas/WriteableResource"},
            ]
        },
    },
}
