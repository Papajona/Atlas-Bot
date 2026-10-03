# ==============================================================================
# ProGuard & R8 Optimization / Obfuscation Rules for Atlas Trading (Release 3.10.47)
# ==============================================================================

# ------------------------------------------------------------------------------
# 1. Code Shrinking & Obfuscation Dictionaries
# ------------------------------------------------------------------------------
-optimizationpasses 5
-allowaccessmodification
-mergeinterfacesaggressively
-overloadaggressively

# Retain stack trace line numbers and source file attributes for crash reporting
-renamesourcefileattribute SourceFile
-keepattributes SourceFile,LineNumberTable,Signature,InnerClasses,EnclosingMethod,*Annotation*

# Strip debug and verbose log calls in production binaries to prevent information leakage
-assumenosideeffects class android.util.Log {
    public static boolean isLoggable(java.lang.String, int);
    public static int v(...);
    public static int d(...);
    public static int i(...);
}

# ------------------------------------------------------------------------------
# 2. Android Framework & Entrypoints
# ------------------------------------------------------------------------------
-keep public class com.atlas.trading.MainActivity {
    public void *(android.view.View);
    public <init>();
}

-keep public class * extends android.app.Activity
-keep public class * extends android.app.Application
-keep public class * extends android.app.Service
-keep public class * extends android.content.BroadcastReceiver
-keep public class * extends android.content.ContentProvider

# Keep view constructors invoked via XML layout inflation
-keepclasseswithmembers class * {
    public <init>(android.content.Context, android.util.AttributeSet);
}
-keepclasseswithmembers class * {
    public <init>(android.content.Context, android.util.AttributeSet, int);
}

# ------------------------------------------------------------------------------
# 3. AndroidX, AppCompat, and WebKit Support
# ------------------------------------------------------------------------------
-keep class androidx.appcompat.** { *; }
-keep class androidx.core.** { *; }
-keep class androidx.webkit.** { *; }
-dontwarn androidx.webkit.**

# Preserve JavaScript Interfaces if WebKit interaction is attached
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
-keepattributes JavascriptInterface

# ------------------------------------------------------------------------------
# 4. Data Serialization & Model Protection
# ------------------------------------------------------------------------------
# Prevent field renaming or stripping on data transport models
-keepclassmembers class * implements java.io.Serializable {
    static final long serialVersionUID;
    private static final java.io.ObjectStreamField[] serialPersistentFields;
    !static !transient <fields>;
    !private <fields>;
    !private <methods>;
    private void writeObject(java.io.ObjectOutputStream);
    private void readObject(java.io.ObjectInputStream);
    java.lang.Object writeReplace();
    java.lang.Object readResolve();
}

# Keep Parcelable creators
-keepclassmembers class * implements android.os.Parcelable {
    public static final ** CREATOR;
}

# Suppress warnings on optional dependencies
-dontwarn java.lang.invoke.**
-dontwarn javax.annotation.**

# OkHttp & Okio rules
-dontwarn okhttp3.**
-dontwarn okio.**
-keepnames class okhttp3.internal.publicsuffix.PublicSuffixDatabase

# Biometric rules
-dontwarn androidx.biometric.**
-keep class androidx.biometric.** { *; }
