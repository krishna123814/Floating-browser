#!/usr/bin/env sh

APP_NAME="Gradle"
APP_BASE_NAME=`basename "$0"`

CLASSPATH=$APP_HOME/gradle/wrapper/gradle-wrapper.jar

exec "$JAVACMD" "$@" -classpath "$CLASSPATH" org.gradle.wrapper.GradleWrapperMain "$@"
