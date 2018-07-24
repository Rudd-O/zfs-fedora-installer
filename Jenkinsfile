import org.jenkinsci.plugins.pipeline.modeldefinition.Utils

// https://github.com/Rudd-O/shared-jenkins-libraries
@Library('shared-jenkins-libraries@master') _
pipeline {

	agent none

	options {
		checkoutToSubdirectory 'src/zfs-fedora-installer'
	}

	triggers {
		pollSCM('H * * * *')
		upstream(
			upstreamProjects: 'ZFS/master,ZFS/staging',
			threshold: hudson.model.Result.SUCCESS
		)
	}

	parameters {
		string defaultValue: 'ZFS (master)', description: '', name: 'UPSTREAM_PROJECT', trim: true
		string defaultValue: 'master', description: '', name: 'SOURCE_BRANCH', trim: true
		string defaultValue: 'yes', description: '', name: 'BUILD_FROM_SOURCE', trim: true
		string defaultValue: 'yes', description: '', name: 'BUILD_FROM_RPMS', trim: true
		string defaultValue: 'seed', description: '', name: 'POOL_NAME', trim: true
		string defaultValue: 'seed.dragonfear', description: '', name: 'HOST_NAME', trim: true
		choice choices: ['never', 'beginning', 'reload_chroot', 'prepare_bootloader_install', 'boot_to_install_bootloader', 'boot_to_test_hostonly'], description: '', name: 'BREAK_BEFORE'
		string defaultValue: 'yes no', description: '', name: 'SEPARATE_BOOT', trim: true
		string defaultValue: 'yes no', description: '', name: 'LUKS', trim: true
		string defaultValue: '23 27', description: '', name: 'RELEASE', trim: true
	}

	stages {
		stage('Preparation') {
			agent { label 'master' }
			steps {
				script {
					funcs.announceBeginning()
				}
				script {
					env.GIT_HASH = sh (
						script: "cd src/zfs-fedora-installer && git rev-parse --short HEAD",
						returnStdout: true
					).trim()
					println "Git hash is reported as ${env.GIT_HASH}"
				}
			}
		}
		stage('Setup environment') {
			agent { label 'master' }
			steps {
				script {
					def upstream = currentBuild.rawBuild.getCause(hudson.model.Cause$UpstreamCause)
					if (upstream != null) {
						if (env.BRANCH_NAME != "master") {
							currentBuild.description = "Skipped test triggered by upstream job ${upstream.upstreamProject} because this test is from the ${env.BRANCH_NAME} branch of zfs-fedora-installer."
							currentBuild.result = 'NOT_BUILT'
							return
						}
						env.BUILD_TRIGGER = "triggered by upstream job " + upstream.upstreamProject
						env.UPSTREAM_PROJECT = upstream.upstreamProject
						env.SOURCE_BRANCH = ""
						env.BUILD_FROM_SOURCE = "no"
						env.BUILD_FROM_RPMS = "yes"
					} else {
						env.BUILD_TRIGGER = "triggered manually"
						env.UPSTREAM_PROJECT = params.UPSTREAM_PROJECT
						env.SOURCE_BRANCH = params.SOURCE_BRANCH
						env.BUILD_FROM_SOURCE = params.BUILD_FROM_SOURCE
						env.BUILD_FROM_RPMS = params.BUILD_FROM_RPMS
					}
					if (env.UPSTREAM_PROJECT == "") {
						currentBuild.result = 'ABORTED'
						error("UPSTREAM_PROJECT must be set to a project containing built ZFS RPMs.")
					}
					if (env.BUILD_FROM_SOURCE == "yes" && env.BUILD_FROM_RPMS == "yes") {
						env.BUILD_FROM = "source RPMs"
					} else if (env.BUILD_FROM_SOURCE == "yes" && env.BUILD_FROM_RPMS == "no") {
						env.BUILD_FROM = "source"
					} else if (env.BUILD_FROM_SOURCE == "no" && env.BUILD_FROM_RPMS == "yes") {
						env.BUILD_FROM = "RPMs"
					} else {
						currentBuild.result = 'ABORTED'
						error("At least one of BUILD_FROM_SOURCE and BUILD_FROM_RPMS must be set to yes.")
					}
					if (env.BUILD_FROM_SOURCE == "yes" && env.SOURCE_BRANCH == "") {
						currentBuild.result = 'ABORTED'
						error("SOURCE_BRANCH must be set when BUILD_FROM_SOURCE is set to yes.")
					}
					currentBuild.description = "Test of ${env.BUILD_FROM} from source branch ${env.SOURCE_BRANCH} and RPMs from ${env.UPSTREAM_PROJECT} ${env.BUILD_TRIGGER}."
				}
			}
		}
		stage('Copy from master') {
			agent { label 'master' }
			when { not { equals expected: 'NOT_BUILT', actual: currentBuild.result } }
			steps {
					copyArtifacts(
						projectName: env.UPSTREAM_PROJECT,
						fingerprintArtifacts: true
					)
					sh '''#!/bin/bash -xe
					find dist/RELEASE=* -type f | sort | grep -v debuginfo | xargs sha256sum > rpmsums
					'''
					sh '''#!/bin/bash -xe
					cp -a "$JENKINS_HOME"/userContent/activate-zfs-in-qubes-vm .
					'''
					stash includes: 'dist/RELEASE=*/**', name: 'rpms', excludes: '**/*debuginfo*'
					stash includes: 'rpmsums', name: 'rpmsums'
					stash includes: 'activate-zfs-in-qubes-vm', name: 'activate-zfs-in-qubes-vm'
					stash includes: 'src/zfs-fedora-installer/**', name: 'zfs-fedora-installer'
				}
			}
		}
		stage('Parallelize') {
			agent { label 'master' }
			when { not { equals expected: 'NOT_BUILT', actual: currentBuild.result } }
			steps {
				script {
					def axisList = [
						params.SEPARATE_BOOT.split(' '),
						params.LUKS.split(' '),
						env.BUILD_FROM.split(' '),
						params.RELEASE.split(' '),
					]
					def task = {
						def mySeparateBoot = it[0]
						def myLuks = it[1]
						def myBuildFrom = it[2]
						def myRelease = it[3]
						return {
							node('fedorazfs') {
								stage("Do ${it.join(' ')}") {
									stage("Install deps ${it.join(' ')}") {
										println "Install deps ${it.join(' ')}"
										timeout(time: 10, unit: 'MINUTES') {
											retry(2) {
												sh '''#!/bin/bash -xe
													(
														flock 9
														deps="rsync e2fsprogs dosfstools cryptsetup qemu gdisk python2"
														rpm -q $deps || sudo dnf install -qy $deps
													) 9> /tmp/$USER-dnf-lock
												'''
											}
										}
									}
									stage("Activate ZFS ${it.join(' ')}") {
										println "Setup ${it.join(' ')}"
										timeout(time: 10, unit: 'MINUTES') {
											unstash "activate-zfs-in-qubes-vm"
											unstash "rpmsums"
											def needsunstash = sh (
												script: '''
												set +e
												set +x
												output=$(sha256sum -c < rpmsums 2>&1)
												if [ "$?" == "0" ]
												then
												    echo MATCH
												else
												    echo "$output" >&2
												fi
												''',
												returnStdout: true
											).trim()
											if (needsunstash != "MATCH") {
												unstash "rpms"
											}
											retry(5) {
												sh '''#!/bin/bash -xe
													release=`rpm -q --queryformat="%{version}" fedora-release`
													sudo ./activate-zfs-in-qubes-vm dist/RELEASE=$release/
												'''
											}
										}
									}
									stage("Build image ${it.join(' ')}") {
										println "Build ${it.join(' ')}"
										timeout(time: 60, unit: 'MINUTES') {
											unstash "zfs-fedora-installer"
											def program = """
												#!/bin/bash -xe
												if [ "${myBuildFrom}" == "RPMs" ] ; then
												  prebuiltrpms=--use-prebuilt-rpms=dist/RELEASE=${myRelease}/
												else
												  prebuiltrpms=
												fi
												if [ "${env.SOURCE_BRANCH}" != "" ] ; then
												  usebranch=--use-branch=${env.SOURCE_BRANCH}
												else
												  usebranch=
												fi
												if [ "${env.CLEANUP_ON_ERRORS}" == "false" ] ; then
												  cleanuponerrors=--no-cleanup
												else
												  cleanuponerrors=
												fi
												if [ "${env.BREAK_BEFORE}" != "never" ] ; then
												  breakbefore=--break-before="${env.BREAK_BEFORE}"
												else
												  breakbefore=
												fi
												yumcache="\$JENKINS_HOME/yumcache"
												volsize=10000
												cmd=src/zfs-fedora-installer/install-fedora-on-zfs
												pname="${env.POOL_NAME}"_"${env.GIT_HASH}"_"${myRelease}"_"${myBuildFrom}"_"${myLuks}"_"${mySeparateBoot}"
												if [ "${myLuks}" == "yes" ] ; then
												  lukspassword=--luks-password=seed
												else
												  lukspassword=
												fi
												if [ "${mySeparateBoot}" == "yes" ] ; then
												  separateboot=--separate-boot=boot-\$pname.img
												else
												  separateboot=
												fi
												pid=
												_term() {
												    echo "SIGTERM received" >&2
												    if [ -n "\$pid" ] ; then
												        echo "Killing PID \$pid with SIGINT" >&2
												        sudo kill -INT "\$pid"
												    fi
												}
												sudo "\$cmd" \
												  \$prebuiltrpms \
												  \$cleanuponerrors \
												  \$breakbefore \
												  \$usebranch \
												  --releasever=${myRelease} \
												  --yum-cachedir="\$yumcache" \
												  --host-name="\$HOST_NAME" \
												  --pool-name="\$pname" \
												  --vol-size=\$volsize \
												  --swap-size=256 \
												  --root-password=seed \
												  \$lukspassword \
												  \$separateboot \
												  --chown="\$USER" \
												  --chgrp=`groups | cut -d " " -f 1` \
												  --luks-options='-c aes-xts-plain64:sha256 -h sha256 -s 512 --use-random --align-payload 4096' \
												  root-\$pname.img >&2 &
												pid=\$!
												retval=0
												trap _term SIGTERM
												wait \$pid || retval=\$?
												# cleanup
												if [ "${env.BREAK_BEFORE}" == "never" ] ; then
												  if [ "\$retval" == "0" -o "${env.CLEANUP_ON_ERRORS}" == "true" ] ; then
												    rm -rf root-\$pname.img boot-\$pname.img
												  fi
												fi
												exit \$retval
											""".stripIndent().trim()
											println "Program that will be executed:"
											println program
											retry(2) {
												sh program
											}
										}
									}
								}
							}
						}
					}
					parallel funcs.combo(task, axisList)
				}
			}
		}
	}
	post {
		always {
			node('master') {
				script {
					funcs.announceEnd(currentBuild.currentResult)
				}
			}
		}
	}
}
