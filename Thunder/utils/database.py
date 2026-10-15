# Thunder/utils/database.py

import datetime
from typing import Any, Dict, Optional
from pymongo import AsyncMongoClient
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.errors import DuplicateKeyError
from pymongo.read_preferences import (
    Primary, PrimaryPreferred, Secondary, SecondaryPreferred, Nearest
)
from Thunder.vars import Var
from Thunder.utils.logger import logger

class Database:
    def __init__(self, uri: str, database_name: str, *args, **kwargs):
        self._client = AsyncMongoClient(
            uri,
            maxPoolSize=Var.MONGO_MAX_POOL_SIZE,
            minPoolSize=Var.MONGO_MIN_POOL_SIZE,
            connectTimeoutMS=Var.MONGO_CONNECT_TIMEOUT_MS,
            serverSelectionTimeoutMS=Var.MONGO_SERVER_SELECTION_TIMEOUT_MS,
            read_preference={
                "primary": Primary(),
                "primaryPreferred": PrimaryPreferred(),
                "secondary": Secondary(),
                "secondaryPreferred": SecondaryPreferred(),
                "nearest": Nearest(),
            }.get(Var.MONGO_READ_PREFERENCE, SecondaryPreferred()),
            *args, **kwargs
        )
        self.db = self._client[database_name]
        self.col: AsyncCollection = self.db.users
        self.banned_users_col: AsyncCollection = self.db.banned_users
        self.banned_channels_col: AsyncCollection = self.db.banned_channels
        self.token_col: AsyncCollection = self.db.tokens
        self.authorized_users_col: AsyncCollection = self.db.authorized_users
        self.restart_message_col: AsyncCollection = self.db.restart_message
        self.files_col: AsyncCollection = self.db.files
        self.file_ingest_locks_col: AsyncCollection = self.db.file_ingest_locks
        self.banned_ips_col: AsyncCollection = self.db.banned_ips

    async def _deduplicate_users(self) -> None:
        pipeline = [
            {"$sort": {"join_date": 1}},
            {"$group": {"_id": "$id", "doc_id": {"$first": "$_id"}}},
            {"$project": {"_id": "$doc_id"}}
        ]
        keep_ids = []
        async for doc in self.col.aggregate(pipeline):
            keep_ids.append(doc["_id"])
        if keep_ids:
            result = await self.col.delete_many({"_id": {"$nin": keep_ids}})
            if result.deleted_count > 0:
                logger.warning(f"Deduplicated {result.deleted_count} duplicate user documents.")

    async def ensure_indexes(self, *, raise_on_error: bool = True) -> bool:
        try:
            await self.banned_users_col.create_index("user_id", unique=True)
            await self.banned_channels_col.create_index("channel_id", unique=True)
            await self.token_col.create_index("token", unique=True)
            await self.authorized_users_col.create_index("user_id", unique=True)
            try:
                await self.col.create_index("id", unique=True)
            except DuplicateKeyError:
                logger.warning("Duplicate users found, deduplicating...")
                await self._deduplicate_users()
                await self.col.create_index("id", unique=True)
            await self.token_col.create_index("expires_at", expireAfterSeconds=0)
            await self.restart_message_col.create_index("message_id", unique=True)
            await self.restart_message_col.create_index("timestamp", expireAfterSeconds=3600)
            await self.files_col.create_index("file_unique_id", unique=True)
            await self.files_col.create_index("public_hash", unique=True)
            await self.files_col.create_index("canonical_message_id", unique=True)
            await self.files_col.create_index("created_at")
            await self.token_col.create_index([("user_id", 1), ("activated", 1), ("expires_at", 1)])
            await self.files_col.create_index([("last_seen_at", -1), ("seen_count", -1)])
            await self.file_ingest_locks_col.create_index("expires_at", expireAfterSeconds=0)
            await self.banned_ips_col.create_index("ip", unique=True)

            logger.debug("Database indexes ensured.")
            return True
        except Exception as e:
            logger.error(f"Error in ensure_indexes: {e}", exc_info=True)
            if raise_on_error:
                raise
            return False

    def new_user(self, user_id: int) -> dict:
        try:
            return {
                'id': user_id,
                'join_date': datetime.datetime.now(datetime.timezone.utc)
            }
        except Exception as e:
            logger.error(f"Error in new_user for user {user_id}: {e}", exc_info=True)
            raise

    async def add_user(self, user_id: int) -> bool:
        try:
            result = await self.col.update_one(
                {'id': user_id},
                {'$setOnInsert': self.new_user(user_id)},
                upsert=True
            )
            if result.upserted_id:
                logger.debug(f"Added new user {user_id} to database.")
                return True
            return False
        except Exception as e:
            logger.error(f"Error in add_user for user {user_id}: {e}", exc_info=True)
            raise


    async def is_user_exist(self, user_id: int) -> bool:
        """Read-only existence check. For user registration, use add_user() instead."""
        try:
            user = await self.col.find_one({'id': user_id}, {'_id': 1})
            return bool(user)
        except Exception as e:
            logger.error(f"Error in is_user_exist for user {user_id}: {e}", exc_info=True)
            raise

    async def total_users_count(self) -> int:
        try:
            return await self.col.count_documents({})
        except Exception as e:
            logger.error(f"Error in total_users_count: {e}", exc_info=True)
            return 0

    async def get_authorized_users_count(self) -> int:
        try:
            return await self.authorized_users_col.count_documents({})
        except Exception as e:
            logger.error(f"Error in get_authorized_users_count: {e}", exc_info=True)
            return 0

    async def get_regular_users_count(self) -> int:
        try:
            pipeline = [
                {
                    "$lookup": {
                        "from": "authorized_users",
                        "localField": "id",
                        "foreignField": "user_id",
                        "as": "auth"
                    }
                },
                {"$match": {"auth": {"$size": 0}}},
                {"$count": "count"}
            ]
            result = await self.col.aggregate(pipeline, allowDiskUse=True).to_list(length=1)
            return result[0]["count"] if result else 0
        except Exception as e:
            logger.error(f"Error in get_regular_users_count: {e}", exc_info=True)
            return 0

    async def get_all_users(self):
        return self.col.find({}, {"id": 1, "join_date": 1, "_id": 0})

    async def get_authorized_users_cursor(self):
        try:
            return self.authorized_users_col.find({})
        except Exception as e:
            logger.error(f"Error in get_authorized_users_cursor: {e}", exc_info=True)
            return self.authorized_users_col.find({"_id": {"$exists": False}})

    async def get_regular_users_cursor(self):
        try:
            auth_ids = await self.authorized_users_col.distinct("user_id")
            return self.col.find({"id": {"$nin": auth_ids}})
        except Exception as e:
            logger.error(f"Error in get_regular_users_cursor: {e}", exc_info=True)
            return self.col.find({"_id": {"$exists": False}})

    async def delete_user(self, user_id: int):
        try:
            await self.col.delete_one({'id': user_id})
            logger.debug(f"Deleted user {user_id}.")
        except Exception as e:
            logger.error(f"Error in delete_user for user {user_id}: {e}", exc_info=True)
            raise


    async def add_banned_user(
        self, user_id: int, banned_by: Optional[int] = None,
        reason: Optional[str] = None
    ):
        try:
            ban_data = {
                "user_id": user_id,
                "banned_at": datetime.datetime.now(datetime.timezone.utc),
                "banned_by": banned_by,
                "reason": reason
            }
            await self.banned_users_col.update_one(
                {"user_id": user_id},
                {"$set": ban_data},
                upsert=True
            )
            logger.debug(f"Added/Updated banned user {user_id}. Reason: {reason}")
        except Exception as e:
            logger.error(f"Error in add_banned_user for user {user_id}: {e}", exc_info=True)
            raise

    async def remove_banned_user(self, user_id: int) -> bool:
        try:
            result = await self.banned_users_col.delete_one({"user_id": user_id})
            if result.deleted_count > 0:
                logger.debug(f"Removed banned user {user_id}.")
                return True
            return False
        except Exception as e:
            logger.error(f"Error in remove_banned_user for user {user_id}: {e}", exc_info=True)
            return False

    async def is_user_banned(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            return await self.banned_users_col.find_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"Error in is_user_banned for user {user_id}: {e}", exc_info=True)
            return None

    async def add_banned_channel(
        self, channel_id: int, banned_by: Optional[int] = None,
        reason: Optional[str] = None
    ):
        try:
            ban_data = {
                "channel_id": channel_id,
                "banned_at": datetime.datetime.now(datetime.timezone.utc),
                "banned_by": banned_by,
                "reason": reason
            }
            await self.banned_channels_col.update_one(
                {"channel_id": channel_id},
                {"$set": ban_data},
                upsert=True
            )
            logger.debug(f"Added/Updated banned channel {channel_id}. Reason: {reason}")
        except Exception as e:
            logger.error(f"Error in add_banned_channel for channel {channel_id}: {e}", exc_info=True)
            raise

    async def remove_banned_channel(self, channel_id: int) -> bool:
        try:
            result = await self.banned_channels_col.delete_one({"channel_id": channel_id})
            if result.deleted_count > 0:
                logger.debug(f"Removed banned channel {channel_id}.")
                return True
            return False
        except Exception as e:
            logger.error(f"Error in remove_banned_channel for channel {channel_id}: {e}", exc_info=True)
            return False

    async def is_channel_banned(self, channel_id: int) -> Optional[Dict[str, Any]]:
        try:
            return await self.banned_channels_col.find_one({"channel_id": channel_id})
        except Exception as e:
            logger.error(f"Error in is_channel_banned for channel {channel_id}: {e}", exc_info=True)
            return None

    async def save_main_token(self, user_id: int, token_value: str, expires_at: datetime.datetime, created_at: datetime.datetime, activated: bool) -> None:
        try:
            await self.token_col.update_one(
                {"user_id": user_id, "token": token_value},
                {"$set": {
                    "expires_at": expires_at,
                    "created_at": created_at,
                    "activated": activated
                    }
                },
                upsert=True
            )
            logger.debug(f"Saved main token {token_value} for user {user_id} with activated status {activated}.")
        except Exception as e:
            logger.error(f"Error saving main token for user {user_id}: {e}", exc_info=True)
            raise


    async def add_restart_message(self, message_id: int, chat_id: int) -> None:
        try:
            await self.restart_message_col.insert_one({
                "message_id": message_id,
                "chat_id": chat_id,
                "timestamp": datetime.datetime.now(datetime.timezone.utc)
            })
            logger.debug(f"Added restart message {message_id} for chat {chat_id}.")
        except Exception as e:
            logger.error(f"Error adding restart message {message_id}: {e}", exc_info=True)

    async def get_restart_message(self) -> Optional[Dict[str, Any]]:
        try:
            return await self.restart_message_col.find_one(sort=[("timestamp", -1)])
        except Exception as e:
            logger.error(f"Error getting restart message: {e}", exc_info=True)
            return None

    async def delete_restart_message(self, message_id: int) -> None:
        try:
            await self.restart_message_col.delete_one({"message_id": message_id})
            logger.debug(f"Deleted restart message {message_id}.")
        except Exception as e:
            logger.error(f"Error deleting restart message {message_id}: {e}", exc_info=True)

    async def is_user_authorized(self, user_id: int) -> bool:
        try:
            user = await self.authorized_users_col.find_one({'user_id': user_id}, {'_id': 1})
            return bool(user)
        except Exception as e:
            logger.error(f"Error in is_user_authorized for user {user_id}: {e}", exc_info=True)
            return False

    async def get_file_by_unique_id(self, file_unique_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.files_col.find_one({"file_unique_id": file_unique_id})
        except Exception as e:
            logger.error(f"Error getting file by unique_id {file_unique_id}: {e}", exc_info=True)
            return None

    async def get_file_by_hash(
        self,
        public_hash: str,
        *,
        raise_on_error: bool = True
    ) -> Optional[Dict[str, Any]]:
        try:
            return await self.files_col.find_one({"public_hash": public_hash})
        except Exception as e:
            logger.error(f"Error getting file by hash {public_hash}: {e}", exc_info=True)
            if raise_on_error:
                raise
            return None

    async def get_file_by_message_id(self, canonical_message_id: int) -> Optional[Dict[str, Any]]:
        try:
            return await self.files_col.find_one({"canonical_message_id": canonical_message_id})
        except Exception as e:
            logger.error(
                f"Error getting file by message_id {canonical_message_id}: {e}",
                exc_info=True
            )
            return None

    async def create_file_record(self, file_record: Dict[str, Any]) -> None:
        try:
            await self.files_col.insert_one(file_record)
        except Exception as e:
            logger.error(
                f"Error creating canonical file record for {file_record.get('file_unique_id')}: {e}",
                exc_info=True
            )
            raise

    async def replace_file_record(self, file_record: Dict[str, Any]) -> None:
        try:
            await self.files_col.replace_one(
                {"file_unique_id": file_record["file_unique_id"]},
                file_record,
                upsert=True
            )
        except Exception as e:
            logger.error(
                f"Error replacing canonical file record for {file_record.get('file_unique_id')}: {e}",
                exc_info=True
            )
            raise

    async def touch_file_record(
        self,
        public_hash: str,
        *,
        reused: bool = False,
        raise_on_error: bool = False
    ) -> bool:
        try:
            update_doc: Dict[str, Any] = {
                "$set": {"last_seen_at": datetime.datetime.now(datetime.timezone.utc)},
                "$inc": {"seen_count": 1}
            }
            if reused:
                update_doc["$inc"]["reuse_count"] = 1
            await self.files_col.update_one({"public_hash": public_hash}, update_doc)
            return True
        except Exception as e:
            logger.error(f"Error touching canonical file {public_hash}: {e}", exc_info=True)
            if raise_on_error:
                raise
            return False

    async def update_file_id(
        self,
        public_hash: str,
        file_id: str,
        *,
        raise_on_error: bool = False
    ) -> bool:
        try:
            await self.files_col.update_one(
                {"public_hash": public_hash},
                {
                    "$set": {
                        "file_id": file_id,
                        "last_seen_at": datetime.datetime.now(datetime.timezone.utc)
                    }
                }
            )
            return True
        except Exception as e:
            logger.error(f"Error updating file_id for {public_hash}: {e}", exc_info=True)
            if raise_on_error:
                raise
            return False

    async def acquire_file_ingest_claim(
        self,
        file_unique_id: str,
        *,
        ttl_seconds: int = 60
    ) -> bool:
        now = datetime.datetime.now(datetime.timezone.utc)
        claim_fields = {
            "created_at": now,
            "expires_at": now + datetime.timedelta(seconds=ttl_seconds)
        }
        try:
            await self.file_ingest_locks_col.insert_one({
                "_id": file_unique_id,
                **claim_fields
            })
            return True
        except DuplicateKeyError:
            try:
                result = await self.file_ingest_locks_col.find_one_and_update(
                    {
                        "_id": file_unique_id,
                        "$or": [
                            {"expires_at": {"$lte": now}},
                            {"expires_at": {"$exists": False}}
                        ]
                    },
                    {
                        "$set": claim_fields
                    },
                    return_document=False
                )
                return bool(result)
            except Exception as e:
                logger.error(f"Error updating ingest claim for {file_unique_id}: {e}", exc_info=True)
                raise
        except Exception as e:
            logger.error(f"Error acquiring ingest claim for {file_unique_id}: {e}", exc_info=True)
            raise

    async def release_file_ingest_claim(self, file_unique_id: str) -> bool:
        try:
            await self.file_ingest_locks_col.delete_one({"_id": file_unique_id})
            return True
        except Exception as e:
            logger.error(f"Error releasing ingest claim for {file_unique_id}: {e}", exc_info=True)
            return False

    async def is_file_ingest_claim_active(self, file_unique_id: str) -> bool:
        try:
            claim = await self.file_ingest_locks_col.find_one(
                {
                    "_id": file_unique_id,
                    "expires_at": {"$gt": datetime.datetime.now(datetime.timezone.utc)}
                },
                {"_id": 1}
            )
            return bool(claim)
        except Exception as e:
            logger.error(f"Error checking ingest claim for {file_unique_id}: {e}", exc_info=True)
            raise

    async def add_banned_ip(
        self,
        ip: str,
        reason: Optional[str] = None,
        banned_by: Optional[int] = None
    ) -> None:
        try:
            await self.banned_ips_col.update_one(
                {"ip": ip},
                {
                    "$set": {
                        "ip": ip,
                        "banned_at": datetime.datetime.now(datetime.timezone.utc),
                        "banned_by": banned_by,
                        "reason": reason
                    }
                },
                upsert=True
            )
        except Exception as e:
            logger.error(f"Error banning ip {ip}: {e}", exc_info=True)
            raise

    async def remove_banned_ip(self, ip: str) -> bool:
        try:
            result = await self.banned_ips_col.delete_one({"ip": ip})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error unbanning ip {ip}: {e}", exc_info=True)
            raise

    async def get_banned_ips(self) -> list:
        try:
            cursor = self.banned_ips_col.find({}, {"_id": 0})
            return await cursor.to_list(length=1000)
        except Exception as e:
            logger.error(f"Error listing banned ips: {e}", exc_info=True)
            return []

    async def set_file_revoked(
        self,
        public_hash: str,
        revoked: bool = True,
        *,
        raise_on_error: bool = False
    ) -> bool:
        try:
            result = await self.files_col.update_one(
                {"public_hash": public_hash},
                {
                    "$set": {
                        "revoked": revoked,
                        "revoked_at": datetime.datetime.now(datetime.timezone.utc) if revoked else None
                    }
                }
            )
            return bool(result.modified_count or result.matched_count)
        except Exception as e:
            logger.error(f"Error setting revoked={revoked} for {public_hash}: {e}", exc_info=True)
            if raise_on_error:
                raise
            return False

    async def set_file_expiry(
        self,
        public_hash: str,
        expires_at,
        *,
        raise_on_error: bool = False
    ) -> bool:
        try:
            result = await self.files_col.update_one(
                {"public_hash": public_hash},
                {"$set": {"expires_at": expires_at}}
            )
            return bool(result.modified_count or result.matched_count)
        except Exception as e:
            logger.error(f"Error setting expiry for {public_hash}: {e}", exc_info=True)
            if raise_on_error:
                raise
            return False

    async def get_source_stats(self, source_chat_id: int) -> Dict[str, Any]:
        """How many links this uploader created, and how often they were used."""
        empty = {"files": 0, "downloads": 0, "bytes": 0}
        if source_chat_id is None:
            return dict(empty)
        try:
            cursor = self.files_col.aggregate([
                {"$match": {"first_source_chat_id": source_chat_id}},
                {
                    "$group": {
                        "_id": None,
                        "files": {"$sum": 1},
                        "bytes": {"$sum": {"$ifNull": ["$file_size", 0]}},
                        "downloads": {"$sum": {"$ifNull": ["$reuse_count", 0]}},
                    }
                },
            ])
            documents = await cursor.to_list(length=1)
        except Exception as e:
            logger.error(f"Error aggregating stats for {source_chat_id}: {e}", exc_info=True)
            return dict(empty)

        if not documents:
            return dict(empty)

        document = documents[0]
        return {
            "files": int(document.get("files", 0) or 0),
            "downloads": int(document.get("downloads", 0) or 0),
            "bytes": int(document.get("bytes", 0) or 0),
        }

    async def close(self):
        if self._client:
            await self._client.close()

db = Database(Var.DATABASE_URL, Var.NAME)
