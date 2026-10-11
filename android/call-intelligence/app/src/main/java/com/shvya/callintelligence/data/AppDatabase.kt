package com.shvya.callintelligence.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import com.shvya.callintelligence.net.AuthStore

@Database(
    entities = [LocalCall::class, SyncQueueItem::class, PendingAction::class],
    version = 2,
    exportSchema = false,
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun callDao(): CallDao

    companion object {
        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "CREATE TABLE IF NOT EXISTS pending_actions (" +
                        "id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
                        "path TEXT NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL, " +
                        "label TEXT NOT NULL, retryCount INTEGER NOT NULL, " +
                        "lastError TEXT NOT NULL, createdAt INTEGER NOT NULL)"
                )
                db.execSQL(
                    "CREATE INDEX IF NOT EXISTS index_pending_actions_createdAt " +
                        "ON pending_actions (createdAt)"
                )
            }
        }

        private const val OWNERSHIP_PREFERENCES = "shvya_local_storage_ownership"
        private const val LEGACY_OWNER_KEY = "legacy_owner"
        private const val LEGACY_GENERATION_KEY = "legacy_generation"
        private val instances = mutableMapOf<String, AppDatabase>()
        private var ownershipCaptureAttempted = false
        private var legacyOwner: String? = null
        private var legacyGeneration: String? = null
        @Volatile var accountStorageReady: Boolean = false
            private set

        /** Called before providers/workers or any new login can run, never during database get. */
        fun captureLegacyOwnership(context: Context, accountId: String?, sessionGeneration: String) {
            synchronized(this) {
                if (ownershipCaptureAttempted) return
                ownershipCaptureAttempted = true
                val prefs = context.getSharedPreferences(OWNERSHIP_PREFERENCES, Context.MODE_PRIVATE)
                val existingOwner = prefs.getString(LEGACY_OWNER_KEY, null)
                if (existingOwner != null) {
                    legacyOwner = existingOwner
                    legacyGeneration = prefs.getString(LEGACY_GENERATION_KEY, null)
                    accountStorageReady = true
                    return
                }
                val capturedOwner = AccountStoragePolicy.captureLegacyOwner(null, accountId)
                // SharedPreferences updates memory even when commit fails. Only a successful
                // durable capture may enable queues or a new login; never trust its memory cache.
                val saved = prefs.edit()
                    .putString(LEGACY_OWNER_KEY, capturedOwner)
                    .putString(LEGACY_GENERATION_KEY, sessionGeneration)
                    .commit()
                if (saved) {
                    legacyOwner = capturedOwner
                    legacyGeneration = sessionGeneration
                    accountStorageReady = true
                }
            }
        }

        fun trackingPreferencesName(accountId: String, sessionGeneration: String): String {
            check(accountStorageReady) { "Restart the app to prepare secure local storage" }
            return AccountStoragePolicy.trackingPreferencesName(
                accountId,
                sessionGeneration,
                legacyOwner,
                legacyGeneration,
            )
        }

        fun get(
            context: Context,
            accountId: String = requireNotNull(AuthStore(context).accountId()) {
                "Sign in before accessing the local call queue"
            },
        ): AppDatabase = synchronized(this) {
            check(accountStorageReady) { "Restart the app to prepare secure local storage" }
            val filename = AccountStoragePolicy.databaseName(accountId, legacyOwner)
            instances.getOrPut(filename) {
                Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    filename,
                ).addMigrations(MIGRATION_1_2)
                    .build()
            }
        }
    }
}
