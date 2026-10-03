package com.atlas.trading.security

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.nio.charset.StandardCharsets
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class SecurityAuditLog(context: Context) {
    data class VerificationResult(val valid: Boolean, val records: Int, val error: String? = null)
    private data class Record(val seq:Long,val ts:Long,val event:String,val detail:String,val prev:String,val hash:String)
    private val file=File(context.applicationContext.noBackupFilesDir,"atlas_security_audit.enc")
    private val lock=Any(); private val random=SecureRandom()

    fun append(event:String, detail:Map<String,Any?>=emptyMap()) {
        require(event.isNotBlank())
        synchronized(lock) {
            val current=read()
            if(!current.first) throw SecurityException(current.third ?: "audit journal corrupted")
            val seq=(current.second.lastOrNull()?.seq ?: 0L)+1
            val prev=current.second.lastOrNull()?.hash.orEmpty()
            val ts=System.currentTimeMillis()
            val d=sanitize(detail).toString()
            val hash=sha256(listOf(seq,ts,event.take(100),d,prev).joinToString("|"))
            val plain=JSONObject().put("version",1).put("sequence",seq).put("recorded_at_epoch_ms",ts)
                .put("event",event.take(100)).put("detail",JSONObject(d)).put("previous_hash",prev).put("event_hash",hash).toString()
            FileOutputStream(file,true).bufferedWriter(StandardCharsets.UTF_8).use { it.append(encrypt(plain)); it.newLine() }
            file.setReadable(false,false); file.setWritable(false,false)
        }
    }
    fun verify():VerificationResult=synchronized(lock){
        val r=read(); VerificationResult(r.first,r.second.size,r.third)
    }
    private fun read():Triple<Boolean,List<Record>,String?> {
        if(!file.exists()) return Triple(true,emptyList(),null)
        val out=mutableListOf<Record>(); var seq=1L; var prev=""
        return try {
            for(line in file.readLines(StandardCharsets.UTF_8)){
                if(line.isBlank()) continue
                val j=JSONObject(decrypt(line)); val s=j.getLong("sequence"); val ts=j.getLong("recorded_at_epoch_ms")
                val e=j.getString("event"); val d=j.getJSONObject("detail").toString(); val p=j.optString("previous_hash",""); val h=j.getString("event_hash")
                if(s!=seq) return Triple(false,out,"audit sequence gap")
                if(p!=prev) return Triple(false,out,"audit hash-chain mismatch")
                val expected=sha256(listOf(s,ts,e,d,p).joinToString("|"))
                if(!MessageDigest.isEqual(expected.toByteArray(),h.toByteArray())) return Triple(false,out,"audit hash mismatch")
                out += Record(s,ts,e,d,p,h); seq++; prev=h
            }
            Triple(true,out,null)
        } catch(_:Throwable){ Triple(false,out,"audit record authentication/parse failure") }
    }
    private fun sanitize(detail:Map<String,Any?>):JSONObject {
        val o=JSONObject()
        detail.toSortedMap().forEach { (k,v) ->
            val l=k.lowercase()
            if(listOf("token","secret","password","authorization","api_key","api_secret","private_key","seed","mnemonic","ciphertext").any{l.contains(it)}) return@forEach
            o.put(k.take(80),when(v){null->JSONObject.NULL;is Boolean,is Int,is Long,is Double,is Float,is String->v;else->v.toString().take(500)})
        }; return o
    }
    private fun encrypt(p:String):String {
        val iv=ByteArray(12); random.nextBytes(iv); val c=Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE,key(),GCMParameterSpec(128,iv)); return Base64.encodeToString(iv+c.doFinal(p.toByteArray()),Base64.NO_WRAP)
    }
    private fun decrypt(x:String):String {
        val b=Base64.decode(x,Base64.NO_WRAP); require(b.size>12); val iv=b.copyOfRange(0,12); val c=Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE,key(),GCMParameterSpec(128,iv)); return String(c.doFinal(b.copyOfRange(12,b.size)),StandardCharsets.UTF_8)
    }
    private fun key():SecretKey {
        val ks=KeyStore.getInstance("AndroidKeyStore").apply{load(null)}; val old=ks.getKey("AtlasSecurityAuditKey.v1",null)
        if(old is SecretKey) return old
        val g=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore")
        g.init(KeyGenParameterSpec.Builder("AtlasSecurityAuditKey.v1",KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setKeySize(256).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setRandomizedEncryptionRequired(true).build())
        return g.generateKey()
    }
    private fun sha256(s:String)=MessageDigest.getInstance("SHA-256").digest(s.toByteArray()).joinToString(""){"%02x".format(it)}
}
