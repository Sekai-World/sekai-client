"""Array-to-dict key structures and APP_VER compatibility helpers."""

from copy import deepcopy
from functools import cmp_to_key, lru_cache
from itertools import zip_longest
from os import getenv

BASE_STRUCTURES = {
    "actionSets": [
        "id",
        "areaId",
        "actionSetType",
        "isNextGrade",
        "scenarioId",
        "scriptId",
        "characterIds",
        "archiveDisplayType",
        "archivePublishedAt",
        "releaseConditionId",
    ],
    "areaItemLevels": [
        "areaItemId",
        "level",
        "targetUnit",
        "targetCardAttr",
        "targetGameCharacterId",
        "power1BonusRate",
        "power1AllMatchBonusRate",
        "power2BonusRate",
        "power2AllMatchBonusRate",
        "power3BonusRate",
        "power3AllMatchBonusRate",
        "sentence",
    ],
    "bondsHonors": [
        "id",
        "seq",
        "bondsGroupId",
        "gameCharacterUnitId1",
        "gameCharacterUnitId2",
        "honorRarity",
        "name",
        "pronunciation",
        "description",
        ["levels", ["id", "bondsHonorId", "level", "description"]],
        "configurableUnitVirtualSinger",
    ],
    "cardCostume3ds": ["cardId", "costume3dId"],
    "cardEpisodes": [
        "id",
        "cardId",
        "title",
        "scenarioId",
        "releaseConditionId",
        "power1BonusFixed",
        "power2BonusFixed",
        "power3BonusFixed",
        ["costs", ["resourceId", "resourceType", "resourceLevel", "quantity"]],
        "cardEpisodePartType",
    ],
    "cards": [
        "id",
        "seq",
        "characterId",
        "cardRarityType",
        "specialTrainingPower1BonusFixed",
        "specialTrainingPower2BonusFixed",
        "specialTrainingPower3BonusFixed",
        "attr",
        "supportUnit",
        "skillId",
        "cardSkillName",
        "prefix",
        "assetbundleName",
        "gachaPhrase",
        "archiveDisplayType",
        "archivePublishedAt",
        "cardParameters",
        [
            "specialTrainingCosts",
            [
                "cardId",
                "seq",
                ["cost", ("resourceId", "resourceType", "resourceLevel", "quantity")],
            ],
        ],
        ["masterLessonAchieveResources", ["masterRank", "resources"]],
        "releaseAt",
        "specialTrainingSkillId",
        "specialTrainingSkillName",
        "cardSupplyId",
    ],
    "challengeLiveHighScoreRewards": [
        "id",
        "characterId",
        "highScore",
        "resourceBoxId",
    ],
    "challengeLiveStages": ["characterId", "rank", "nextStageChallengePoint"],
    "character3ds": [
        "id",
        "characterId",
        "unit",
        "headCostume3dId",
        "hairCostume3dId",
        "bodyCostume3dId",
        "lookAtLimitX",
        "lookAtLimitY",
        "name",
    ],
    "characterArchiveVoices": [
        "id",
        "groupId",
        "gameCharacterId",
        "unit",
        "characterArchiveVoiceType",
        "displayPhrase",
        "displayPhrase2",
        "characterArchiveVoiceTagId",
        "externalId",
        "assetName",
        "isNextGrade",
        "displayStartAt",
    ],
    "characterRanks": [
        "id",
        "characterId",
        "characterRank",
        "power1BonusRate",
        "power2BonusRate",
        "power3BonusRate",
        "rewardResourceBoxIds",
        ["characterRankAchieveResources", ["resources"]],
    ],
    "cheerfulCarnivalPartyNames": [
        "id",
        "partyName",
        "gameCharacterUnitId1",
        "gameCharacterUnitId2",
        "gameCharacterUnitId3",
        "gameCharacterUnitId4",
        "gameCharacterUnitId5",
    ],
    "episodeCharacters": ["id", "seq", "character2dId", "storyType", "episodeId"],
    "eventDeckBonuses": [
        "id",
        "eventId",
        "gameCharacterUnitId",
        "cardAttr",
        "bonusRate",
    ],
    "eventExchangeSummaries": [
        "id",
        "eventId",
        "startAt",
        "endAt",
        [
            "eventExchanges",
            [
                "id",
                "eventExchangeSummaryId",
                "gameCharacterId",
                "seq",
                "resourceBoxId",
                "exchangeLimit",
                ["eventExchangeCost", ("resourceQuantity",)],
            ],
        ],
        "assetbundleName",
    ],
    "events": [
        "id",
        "eventType",
        "name",
        "assetbundleName",
        "bgmAssetbundleName",
        "eventPointAssetbundleName",
        "eventOnlyComponentDisplayStartAt",
        "startAt",
        "aggregateAt",
        "rankingAnnounceAt",
        "distributionStartAt",
        "eventOnlyComponentDisplayEndAt",
        "closedAt",
        "virtualLiveId",
        "unit",
        [
            "eventRankingRewardRanges",
            ["fromRank", "toRank", ["eventRankingRewards", ["resourceBoxId"]]],
        ],
        "distributionEndAt",
    ],
    "eventStories": [
        "id",
        "eventId",
        "outline",
        "bannerGameCharacterUnitId",
        "assetbundleName",
        [
            "eventStoryEpisodes",
            [
                "id",
                "eventStoryId",
                "gameCharacterId",
                "episodeNo",
                "title",
                "assetbundleName",
                "scenarioId",
                "releaseConditionId",
                ["episodeRewards", ["startAt", "endAt", "resourceBoxId"]],
            ],
        ],
    ],
    "gachaCeilExchangeSummaries": [
        "id",
        "seq",
        "assetbundleName",
        "startAt",
        "endAt",
        [
            "gachaCeilExchanges",
            [
                "id",
                "gachaCeilExchangeSummaryId",
                "seq",
                "resourceBoxId",
                "exchangeLimit",
                "gachaCeilExchangeLabelType",
                "substituteLimit",
                ["gachaCeilExchangeCost", ("quantity", "resourceType", "resourceId")],
                [
                    "gachaCeilExchangeSubstituteCosts",
                    ["id", "resourceType", "resourceId", "substituteQuantity"],
                ],
            ],
        ],
        "gachaCeilItemId",
    ],
    "gachas": [
        "id",
        "gachaType",
        "name",
        "seq",
        "assetbundleName",
        "startAt",
        "endAt",
        "isShowPeriod",
        "spinLimit",
        "gachaCeilItemId",
        "wishSelectCount",
        "wishFixedSelectCount",
        "wishLimitedSelectCount",
        "gachaBonusId",
        "drawableGachaHour",
        ["gachaCardRarityRates", ["cardRarityType", "lotteryType", "rate"]],
        [
            "gachaDetails",
            [
                "id",
                "gachaId",
                "cardId",
                "weight",
                "fixedBonusWeight",
                "isWish",
                "gachaDetailWishType",
            ],
        ],
        [
            "gachaBehaviors",
            [
                "id",
                "gachaId",
                "gachaBehaviorType",
                "costResourceType",
                "costResourceId",
                "costResourceQuantity",
                "spinCount",
                "executeLimit",
                "gachaExtraId",
                "groupId",
                "priority",
                "resourceCategory",
                "gachaSpinnableType",
            ],
        ],
        ["gachaPickups", ["gachaId", "cardId"]],
        [
            "gachaInformation",
            (
                "gachaId",
                "summary",
                "description",
                "bubbleAssetbundleName",
                "bubbleText",
            ),
        ],
        "dailySpinLimit",
        "gachaBonusItemReceivableRewardGroupId",
        "gachaFreebieGroupId",
    ],
    "honors": [
        "id",
        "seq",
        "groupId",
        "honorRarity",
        "name",
        "assetbundleName",
        "honorTypeId",
        "honorMissionType",
        "startAt",
        [
            "levels",
            [
                "level",
                "bonus",
                "description",
                "startAt",
                "assetbundleName",
                "honorRarity",
            ],
        ],
    ],
    "liveMissions": [
        "id",
        "liveMissionPeriodId",
        "liveMissionType",
        "requirement",
        ["rewards", ["resourceBoxId"]],
    ],
    "masterLessonRewards": ["cardId", "masterRank", "resourceBoxId", "id"],
    "materialExchangeSummaries": [
        "id",
        "seq",
        "exchangeCategory",
        "materialExchangeType",
        "name",
        "assetbundleName",
        "startAt",
        "endAt",
        "notificationRemainHour",
        [
            "materialExchanges",
            [
                "id",
                "materialExchangeSummaryId",
                "seq",
                "displayName",
                "isDisplayQuantity",
                "thumbnailAssetbundleName",
                "resourceBoxId",
                "refreshCycle",
                "exchangeLimit",
                "startAt",
                "endAt",
                [
                    "costs",
                    [
                        "materialExchangeId",
                        "costGroupId",
                        "seq",
                        "resourceId",
                        "quantity",
                    ],
                ],
            ],
        ],
        "materialExchangeDisplayResourceGroupId",
        "materialExchangeDisplayResourceGroups",
        "materialExchangeFreebieGroupJson",
        "materialExchangeFreebies",
    ],
    "musicDifficulties": [
        "id",
        "musicId",
        "musicDifficulty",
        "playLevel",
        "releaseConditionId",
        "totalNoteCount",
    ],
    "musics": [
        "id",
        "seq",
        "releaseConditionId",
        ["categories", ["musicCategoryName", "startAt"]],
        "title",
        "pronunciation",
        "creatorArtistId",
        "lyricist",
        "composer",
        "arranger",
        "dancerCount",
        "selfDancerPosition",
        "assetbundleName",
        "publishedAt",
        "releasedAt",
        "fillerSec",
        ["infos", ["title", "creator", "lyricist", "composer", "arranger"]],
        "musicCollaborationId",
        "isNewlyWrittenMusic",
        "isFullLength",
    ],
    "musicTags": ["musicId", "musicTag"],
    "musicVocals": [
        "id",
        "musicId",
        "musicVocalType",
        "seq",
        "releaseConditionId",
        "caption",
        [
            "characters",
            ["id", "musicId", "musicVocalId", "characterType", "characterId", "seq"],
        ],
        "assetbundleName",
        "specialSeasonId",
        "archiveDisplayType",
        "archivePublishedAt",
    ],
    "ngWords": ["word"],
    "releaseConditions": [
        "id",
        "sentence",
        "releaseConditionType",
        "releaseConditionTypeId",
        "releaseConditionTypeId2",
        "releaseConditionTypeLevel",
        "releaseConditionTypeQuantity",
    ],
    "returnMissions": [
        "returnMissionGroupId",
        "id",
        "seq",
        "returnMissionType",
        "requirement",
        "sentence",
        "resourceBoxId",
    ],
    "shopItems": [
        "id",
        "shopId",
        "seq",
        "releaseConditionId",
        "resourceBoxId",
        [
            "costs",
            [["cost", ("resourceId", "resourceType", "resourceLevel", "quantity")]],
        ],
        "startAt",
        "endAt",
    ],
    "stamps": [
        "id",
        "stampType",
        "seq",
        "name",
        "assetbundleName",
        "balloonAssetbundleName",
        "characterId1",
        "characterId2",
        "characterId3",
        "characterId4",
        "characterId5",
        "gameCharacterUnitId",
        "archiveDisplayType",
        "archivePublishedAt",
        "description",
    ],
    "topics": ["id", "topicType", "topicTypeId", "releaseConditionId"],
    "virtualItems": [
        "id",
        "virtualItemCategory",
        "seq",
        "priority",
        "name",
        "assetbundleName",
        "costVirtualCoin",
        "costJewel",
        "effectAssetbundleName",
        "effectExpressionType",
        "virtualItemLabelType",
        "gameCharacterUnitId",
        "unit",
        "subGameCharacterId",
    ],
    "virtualLives": [
        "id",
        "virtualLiveType",
        "virtualLivePlatform",
        "seq",
        "name",
        "assetbundleName",
        "screenMvMusicVocalId",
        "startAt",
        "endAt",
        "rankingAnnounceAt",
        "archiveReleaseConditionId",
        [
            "virtualLiveSetlists",
            [
                "id",
                "seq",
                "virtualLiveSetlistType",
                "assetbundleName",
                "virtualLiveStageId",
                "musicVocalId",
                "character3dId1",
                "character3dId2",
                "character3dId3",
                "character3dId4",
                "character3dId5",
                "character3dId6",
                "virtualLiveId",
                "musicId",
            ],
        ],
        [
            "virtualLiveBeginnerSchedules",
            ["id", "virtualLiveId", "dayOfWeek", "startTime", "endTime"],
        ],
        [
            "virtualLiveSchedules",
            [
                "id",
                "virtualLiveId",
                "seq",
                "startAt",
                "endAt",
                "noticeGroupId",
                "isAfterEvent",
            ],
        ],
        [
            "virtualLiveCharacters",
            [
                "gameCharacterUnitId",
                "virtualLivePerformanceType",
                "subGameCharacter2dId",
            ],
        ],
        ["virtualLiveRewards", ["virtualLiveType", "resourceBoxId"]],
        ["virtualLiveWaitingRoom", ("id", "lobbyAssetbundleName", "startAt", "endAt")],
        [
            "virtualItems",
            [
                "id",
                "virtualItemCategory",
                "seq",
                "priority",
                "name",
                "assetbundleName",
                "costVirtualCoin",
                "costJewel",
                "effectAssetbundleName",
                "effectExpressionType",
                "virtualItemLabelType",
                "gameCharacterUnitId",
                "unit",
                "subGameCharacterId",
            ],
        ],
        [
            "virtualLiveAppeals",
            ["id", "virtualLiveId", "virtualLiveStageStatus", "appealText"],
        ],
        ["virtualLiveBackgroundMusics", ["id", "virtualLiveId", "backgroundMusicId"]],
        ["virtualLiveInformation", ("virtualLiveId", "summary", "description")],
        "subGameCharacterPenlightColorGroupId",
    ],
    "wordings": ["wordingKey", "value"],
    "sekaiEchoStories": [
        "id",
        "groupId",
        "storyType",
        "eventId",
        "honorGroupId",
        "gameCharacterId",
        "characterEventSeq",
        "musicId",
        "stampId",
        "startAt",
        "unit",
        "assetBundleName",
        "showGameCharacterId",
    ],
    "sekaiEchoStoryGroups": ["groupId", "groupName"],
    "sekaiEchoUnitCharacters": [
        "gameCharacterId",
        "unit",
        "seq",
        "assetBundleName",
    ],
    "sekaiEchoUnitAbs": [
        "unit",
        "seq",
        "assetBundleName",
        "pvAssetBundleName",
        "picAssetBundleName",
    ],
    "sekaiEchoMissions": [
        "id",
        "sekaiEchoMissionType",
        "seq",
        "sentence",
        "requirement",
        ["rewards", ["resourceBoxId"]],
    ],
    "sekaiEchoCardMissions": [
        "cardGroup",
        "sekaiEchoCardMissionType",
        "seq",
        "sentence",
        "keyMission",
        "resourceBoxId",
    ],
    "sekaiEchoHonors": ["eventId", "sekaiLevel", "honorId"],
    "sekaiEchoHonorMissions": [
        "sekaiLevel",
        "sekaiEchoHonorMissionType",
        "seq",
        "requirement",
        "sentence",
    ],
    "sekaiEchoPointExchanges": [
        "resourceType",
        "resourceId",
        "quantity",
        "echoPoint",
        "seq",
    ],
    "shiningExchanges": [
        "id",
        "shiningExchangeType",
        "seq",
        "resourceBoxId",
        "cost",
        "refreshCycle",
        "exchangeLimit",
        "startAt",
        "endAt",
    ],
    "billingShopItems": [
        "id",
        "seq",
        "billingShopItemType",
        "billingProductGroupId",
        "saleType",
        "name",
        "description",
        "billableLimitType",
        "billableLimitValue",
        "billableLimitResetIntervalType",
        "billableLimitResetIntervalValue",
        "assetbundleName",
        "resourceBoxId",
        "bonusResourceBoxId",
        "label",
        "startAt",
        "endAt",
        "purchaseOption",
        "billingShopTabChildId",
    ],
    "ongoingMissions": [
        "id",
        "ongoingMissionType",
        "seq",
        "sentence",
        "requirement",
        ["rewards", ["resourceBoxId"]],
        "startAt",
        "endAt",
    ],
    "jewelShowDialogues": [
        "id",
        "seq",
        "name",
        "assetBundleName",
        "startAt",
        "endAt",
        ["behaviors", ["seq", "billingShopItemId"]],
    ],
    "costume3dFittings": ["billingShopItemId", "characterId", "hairCostume3dId"],
    "costume3dModelDefaultHairs": ["id", "headCostume3dId", "hairCostume3dId", "unit"],
    "costume3dModelNotAvailablePatterns": [
        "id",
        "headCostume3dId",
        "hairCostume3dId",
        "unit",
    ],
}

# Each entry applies from the specified APP_VER onwards.
# Only include structures that changed in that app version.
# Use None to explicitly disable a structure key from that version onwards.
STRUCTURE_COMPATIBILITY: dict[str, dict[str, list | None]] = {
    "5.0.0": {},
    "6.0.0": {
        "cards": [
            "id",
            "seq",
            "characterId",
            "cardRarityType",
            "specialTrainingPower1BonusFixed",
            "specialTrainingPower2BonusFixed",
            "specialTrainingPower3BonusFixed",
            "attr",
            "supportUnit",
            "skillId",
            "cardSkillName",
            "specialTrainingSkillId",
            "specialTrainingSkillName",
            "prefix",
            "assetbundleName",
            "gachaPhrase",
            "releaseAt",
            "archiveDisplayType",
            "archivePublishedAt",
            "cardSupplyId",
            "cardParameters",
            [
                "specialTrainingCosts",
                [
                    "cardId",
                    "seq",
                    [
                        "cost",
                        ("resourceId", "resourceType", "resourceLevel", "quantity"),
                    ],
                ],
            ],
            "specialTrainingRewardResourceBoxId",
            ["masterLessonAchieveResources", ["masterRank", "resources"]],
            "initialSpecialTrainingStatus",
        ],
        "events": [
            "id",
            "eventType",
            "name",
            "assetbundleName",
            "bgmAssetbundleName",
            "eventPointAssetbundleName",
            "eventOnlyComponentDisplayStartAt",
            "standbyScreenDisplayStartAt",
            "startAt",
            "aggregateAt",
            "rankingAnnounceAt",
            "distributionStartAt",
            "eventOnlyComponentDisplayEndAt",
            "closedAt",
            "distributionEndAt",
            "virtualLiveId",
            "unit",
            [
                "eventRankingRewardRanges",
                [
                    "fromRank",
                    "toRank",
                    [
                        "eventRankingRewards",
                        ["resourceBoxId", "rewardConditionType", "conditionValue"],
                    ],
                ],
            ],
        ],
        "materialExchangeSummaries": [
            "id",
            "seq",
            "exchangeCategory",
            "materialExchangeType",
            "materialExchangeDisplayResourceGroupId",
            "name",
            "assetbundleName",
            "startAt",
            "endAt",
            "notificationRemainHour",
            [
                "materialExchanges",
                [
                    "id",
                    "materialExchangeSummaryId",
                    "seq",
                    "displayName",
                    "isDisplayQuantity",
                    "thumbnailAssetbundleName",
                    "resourceBoxId",
                    "refreshCycle",
                    "exchangeLimit",
                    "startAt",
                    "endAt",
                    [
                        "costs",
                        [
                            "materialExchangeId",
                            "costGroupId",
                            "seq",
                            "resourceType",
                            "resourceId",
                            "quantity",
                        ],
                    ],
                    "materialExchangeRelationParents",
                ],
            ],
            "materialExchangeDisplayResourceGroups",
            "materialExchangeFreebieGroupJson",
            "materialExchangeFreebies",
        ],
        "musicDifficulties": [
            "id",
            "musicId",
            "musicDifficulty",
            "playLevel",
            "totalNoteCount",
        ],
        "virtualLives": [
            "id",
            "virtualLiveType",
            "virtualLivePlatform",
            "seq",
            "name",
            "assetbundleName",
            "screenMvMusicVocalId",
            "subGameCharacterPenlightColorGroupId",
            "startAt",
            "endAt",
            "virtualLiveGroupId",
            "rankingAnnounceAt",
            "archiveReleaseConditionId",
            [
                "virtualLiveSetlists",
                [
                    "id",
                    "virtualLiveId",
                    "seq",
                    "virtualLiveSetlistType",
                    "assetbundleName",
                    "virtualLiveStageId",
                    "musicId",
                    "musicVocalId",
                    "character3dId1",
                    "character3dId2",
                    "character3dId3",
                    "character3dId4",
                    "character3dId5",
                    "character3dId6",
                ],
            ],
            [
                "virtualLiveBeginnerSchedules",
                ["id", "virtualLiveId", "dayOfWeek", "startTime", "endTime"],
            ],
            [
                "virtualLiveSchedules",
                [
                    "id",
                    "virtualLiveId",
                    "seq",
                    "startAt",
                    "endAt",
                    "noticeGroupId",
                    "isAfterEvent",
                ],
            ],
            [
                "virtualLiveCharacters",
                [
                    "gameCharacterUnitId",
                    "subGameCharacter2dId",
                    "virtualLivePerformanceType",
                ],
            ],
            ["virtualLiveRewards", ["virtualLiveType", "resourceBoxId"]],
            [
                "virtualLiveWaitingRoom",
                ("id", "lobbyAssetbundleName", "startAt", "endAt"),
            ],
            [
                "virtualItems",
                [
                    "id",
                    "virtualItemLabelType",
                ],
            ],
            [
                "virtualLiveAppeals",
                ["id", "virtualLiveId", "virtualLiveStageStatus", "appealText"],
            ],
            [
                "virtualLiveBackgroundMusics",
                ["id", "virtualLiveId", "backgroundMusicId"],
            ],
            ["virtualLiveInformation", ("virtualLiveId", "summary", "description")],
        ],
        "characterArchiveMysekaiCharacterTalkGroups": [
            "id",
            "seq",
            "archiveDisplayType",
            "characterArchiveMysekaiCharacterTalkGroupTagId",
        ],
        "costume3dColors": ["id", "name"],
        "costume3dGroups": [
            "groupId",
            "name",
            "characterId",
            "rarity",
            "howToObtain",
            "designer",
            "publishedAt",
            "archiveDisplayType",
            "archivePublishedAt",
        ],
        "costume3ds": [
            "id",
            "seq",
            "costume3dGroupId",
            "costume3dType",
            "name",
            "partType",
            "colorId",
            "colorName",
            "characterId",
            "costume3dRarity",
            "howToObtain",
            "_assetbundleName",
            "designer",
            "publishedAt",
            "archiveDisplayType",
            "archivePublishedAt",
        ],
        "customProfileCollectionResources": [
            "customProfileResourceType",
            "id",
            "seq",
            "name",
            "pronunciation",
            "resourceLoadType",
            "resourceLoadVal",
            "fileName",
            "characterId",
            "customProfileResourceCollectionType",
            "groupId",
        ],
        "customProfileEtcResources": [
            "customProfileResourceType",
            "id",
            "seq",
            "name",
            "pronunciation",
            "resourceLoadType",
            "resourceLoadVal",
            "fileName",
            "characterId",
            "customProfileResourceCollectionType",
            "groupId",
        ],
        "customProfileGachas": [
            "id",
            "name",
            "startAt",
            "endAt",
            "assetbundleName",
            "description",
            "notice",
            "customProfileGachaCategory",
            [
                "customProfileGachaBehaviors",
                [
                    "id",
                    "seq",
                    "costResourceType",
                    "costResourceId",
                    "costResourceQuantity",
                    "spinCount",
                ],
            ],
            [
                "customProfileGachaDetails",
                [
                    "id",
                    "customProfileResourceType",
                    "customProfileResourceId",
                    "weight",
                ],
            ],
        ],
        "customProfileGeneralBackgroundResources": [
            "customProfileResourceType",
            "id",
            "seq",
            "name",
            "pronunciation",
            "resourceLoadType",
            "resourceLoadVal",
            "fileName",
            "characterId",
            "customProfileResourceCollectionType",
            "groupId",
        ],
        "customProfileMemberStandingPictureResources": [
            "customProfileResourceType",
            "id",
            "seq",
            "name",
            "pronunciation",
            "resourceLoadType",
            "resourceLoadVal",
            "fileName",
            "characterId",
            "customProfileResourceCollectionType",
            "groupId",
        ],
        "customProfilePlayerInfoResources": [
            "customProfileResourceType",
            "id",
            "seq",
            "name",
            "pronunciation",
            "resourceLoadType",
            "resourceLoadVal",
            "fileName",
            "characterId",
            "customProfileResourceCollectionType",
            "groupId",
        ],
        "customProfileShapeResources": [
            "customProfileResourceType",
            "id",
            "seq",
            "name",
            "pronunciation",
            "resourceLoadType",
            "resourceLoadVal",
            "fileName",
            "characterId",
            "customProfileResourceCollectionType",
            "groupId",
        ],
        "customProfileStoryBackgroundResources": [
            "customProfileResourceType",
            "id",
            "seq",
            "name",
            "pronunciation",
            "resourceLoadType",
            "resourceLoadVal",
            "fileName",
            "characterId",
            "customProfileResourceCollectionType",
            "groupId",
        ],
        "honorMissions": [
            "id",
            "seq",
            "honorMissionType",
            "requirement",
            "sentence",
            ["rewards", ["resourceBoxId"]],
            "startAt",
        ],
        "mysekaiBlueprintMysekaiMaterialCosts": [
            "id",
            "mysekaiBlueprintId",
            "mysekaiMaterialId",
            "seq",
            "quantity",
        ],
        "mysekaiCharacterTalkPreActions": [
            "id",
            "mysekaiCharacterTalkId",
            "mysekaiCharacterTalkTweetId",
            "mysekaiCharacterTalkFixtureTimelineGroupId",
            "mysekaiCharacterTalkFixtureTogetherCommunicationId",
        ],
        "mysekaiCharacterTalkTweets": [
            "id",
            "motionName",
            "emoticonName",
            "expressionEyeName",
            "expressionMouthName",
            "text",
        ],
        "mysekaiCharacterTalks": [
            "id",
            "mysekaiGameCharacterUnitGroupId",
            "mysekaiCharacterTalkConditionGroupId",
            "mysekaiSiteGroupId",
            "mysekaiCharacterTalkTermId",
            "characterArchiveMysekaiCharacterTalkGroupId",
            "assetbundleName",
            "lua",
            "isEnabledForMulti",
        ],
        "mysekaiFixtures": [
            "id",
            "mysekaiFixtureType",
            "name",
            "pronunciation",
            "flavorText",
            "seq",
            "gridSize",
            "colorCode",
            "mysekaiFixtureMainGenreId",
            "mysekaiFixtureSubGenreId",
            "mysekaiFixtureHandleType",
            "mysekaiSettableSiteType",
            "mysekaiSettableLayoutType",
            "mysekaiFixturePutType",
            ["mysekaiFixtureAnotherColors", ["textureId", "colorCode"]],
            "mysekaiFixtureGameCharacterGroupPerformanceBonusId",
            "mysekaiFixturePutSoundId",
            "mysekaiFixtureFootstepId",
            "mysekaiFixtureTagGroup",
            "isAssembled",
            "isDisassembled",
            "mysekaiFixturePlayerActionType",
            "isGameCharacterAction",
            "assetbundleName",
        ],
        "mysekaiGameCharacterUnitGroups": [
            "id",
            "gameCharacterUnitId1",
            "gameCharacterUnitId2",
            "gameCharacterUnitId3",
            "gameCharacterUnitId4",
            "gameCharacterUnitId5",
        ],
    },
}


def _parse_semantic_version(
    version: str,
) -> tuple[tuple[int, ...], tuple[tuple[int, int | str], ...]]:
    normalized_version = version.strip()
    if not normalized_version:
        raise ValueError("version must not be empty")

    version_without_build = normalized_version.split("+", 1)[0]
    core_part, _, prerelease_part = version_without_build.partition("-")
    core_version = tuple(int(part) for part in core_part.split("."))
    prerelease_version = []

    if prerelease_part:
        for part in prerelease_part.split("."):
            if part.isdigit():
                prerelease_version.append((0, int(part)))
            else:
                prerelease_version.append((1, part))

    return core_version, tuple(prerelease_version)


def _compare_semantic_versions(left: str, right: str) -> int:
    left_core, left_prerelease = _parse_semantic_version(left)
    right_core, right_prerelease = _parse_semantic_version(right)

    for left_part, right_part in zip_longest(left_core, right_core, fillvalue=0):
        if left_part < right_part:
            return -1
        if left_part > right_part:
            return 1

    if not left_prerelease and not right_prerelease:
        return 0
    if not left_prerelease:
        return 1
    if not right_prerelease:
        return -1

    for left_part, right_part in zip_longest(
        left_prerelease, right_prerelease, fillvalue=None
    ):
        if left_part is None:
            return -1
        if right_part is None:
            return 1
        if left_part[0] < right_part[0]:
            return -1
        if left_part[0] > right_part[0]:
            return 1
        if left_part[1] < right_part[1]:
            return -1
        if left_part[1] > right_part[1]:
            return 1

    return 0


@lru_cache(maxsize=1)
def _sorted_compatibility_versions() -> tuple[str, ...]:
    return tuple(
        sorted(
            STRUCTURE_COMPATIBILITY.keys(),
            key=cmp_to_key(_compare_semantic_versions),
        )
    )


def resolve_structure_compatibility_version(app_ver: str | None = None) -> str | None:
    target_app_ver = (app_ver or getenv("APP_VER", "")).strip()
    if not target_app_ver:
        return None

    try:
        matched_version = None
        for version in _sorted_compatibility_versions():
            if _compare_semantic_versions(version, target_app_ver) <= 0:
                matched_version = version
            else:
                break
        return matched_version
    except ValueError:
        return None


@lru_cache(maxsize=None)
def _build_structures_for_app_ver(app_ver: str) -> dict[str, list]:
    result = deepcopy(BASE_STRUCTURES)

    for version in _sorted_compatibility_versions():
        if _compare_semantic_versions(version, app_ver) > 0:
            break

        for key, value in STRUCTURE_COMPATIBILITY[version].items():
            if value is None:
                result.pop(key, None)
            else:
                result[key] = deepcopy(value)

    return result


def get_structures_for_app_ver(app_ver: str | None = None) -> dict[str, list]:
    target_app_ver = (app_ver or getenv("APP_VER", "")).strip()
    if not target_app_ver:
        return deepcopy(BASE_STRUCTURES)

    try:
        return deepcopy(_build_structures_for_app_ver(target_app_ver))
    except ValueError:
        return deepcopy(BASE_STRUCTURES)


structures = get_structures_for_app_ver()

__all__ = [
    "BASE_STRUCTURES",
    "STRUCTURE_COMPATIBILITY",
    "get_structures_for_app_ver",
    "resolve_structure_compatibility_version",
    "structures",
]
