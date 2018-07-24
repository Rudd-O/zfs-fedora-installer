import org.jenkinsci.plugins.pipeline.modeldefinition.Utils

pipeline {

	agent none

	options {
		checkoutToSubdirectory 'zfs-fedora-installer'
	}

	triggers {
		pollSCM('H * * * *')
	}

	parameters {
		string defaultValue: 'master', description: '', name: 'RPMS_FROM', trim: true
		booleanParam defaultValue: true, description: '', name: 'CLEANUP_ON_ERRORS'
		string defaultValue: 'seed', description: '', name: 'POOL_NAME', trim: true
		string defaultValue: 'seed.dragonfear', description: '', name: 'HOST_NAME', trim: true
		choice choices: ['never', 'beginning', 'reload_chroot', 'prepare_bootloader_install', 'boot_to_install_bootloader', 'boot_to_test_hostonly'], description: '', name: 'BREAK_BEFORE'
		string defaultValue: 'yes no', description: '', name: 'SEPARATE_BOOT', trim: true
		string defaultValue: 'yes no', description: '', name: 'LUKS', trim: true
		string defaultValue: 'source RPMs', description: '', name: 'BUILD_FROM', trim: true
		string defaultValue: '23 27', description: '', name: 'RELEASE', trim: true
	}

	stages {
		stage('Preparation') {
			agent{ label 'master' }
			steps {
				script {
					env.GIT_COMMIT = sh(
						script: '''cd zfs-fedora-installer && git rev-parse --short HEAD''',
						returnStdout: true
					)
				}
				sh """echo SCM step reports ${env.GIT_COMMIT} as checked out."""
				sh """test -x /usr/local/bin/announce-build-result || exit
				/usr/local/bin/announce-build-result has begun
				"""
			}
		}
		stage('Copy from master') {
			agent{ label 'master' }
			steps {
				script {
					env.UPSTREAM_RPMS = "ZFS (" + params.RPMS_FROM + ")"
				}
				copyArtifacts(projectName: env.UPSTREAM_RPMS)
				sh '''#!/bin/bash -xe
				find RELEASE* -type f | sort | grep -v debuginfo | xargs sha256sum > rpmsums
				'''
				sh '''#!/bin/bash -xe
				cp -a "$JENKINS_HOME"/userContent/activate-zfs-in-qubes-vm .
				'''
				stash includes: 'RELEASE=*/**', name: 'rpms', excludes: '**/*debuginfo*'
				stash includes: 'rpmsums', name: 'rpmsums'
				stash includes: 'activate-zfs-in-qubes-vm', name: 'activate-zfs-in-qubes-vm'
				stash includes: 'zfs-fedora-installer/**', name: 'zfs-fedora-installer'
			}
		}
		stage('Parallelize') {
			agent{ label 'master' }
			steps {
				script {
					def axisList = [
						params.SEPARATE_BOOT.split(' '),
						params.LUKS.split(' '),
						params.BUILD_FROM.split(' '),
						params.RELEASE.split(' '),
					]
					def tasks = [:]
					def comboEntry = []
					def task
					task = {
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
													sudo ./activate-zfs-in-qubes-vm RELEASE=$release/dist/
												'''
											}
										}
									}
									stage("Build image ${it.join(' ')}") {
										println "Build ${it.join(' ')}"
										timeout(time: 60, unit: 'MINUTES') {
											unstash "zfs-fedora-installer"
											def program = """#!/bin/bash -xe
												if [ "${myBuildFrom}" == "RPMs" ] ; then
												  prebuiltrpms=--use-prebuilt-rpms=RELEASE=${myRelease}/dist/
												else
												  prebuiltrpms=
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
												cmd=zfs-fedora-installer/install-fedora-on-zfs
												pname="${env.POOL_NAME}"_"${env.GIT_COMMIT}"_"${myRelease}"_"${myBuildFrom}"_"${myLuks}"_"${mySeparateBoot}"
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
												  --use-branch="\$RPMS_FROM" \
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
											"""
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
					def comboBuilder
					comboBuilder = {
						def axes, int level -> for ( entry in axes[0] ) {
							comboEntry[level] = entry
							if (axes.size() > 1) {
								comboBuilder(axes.drop(1), level + 1)
							}
							else {
								tasks[comboEntry.join("_")] = task(comboEntry.collect())
							}
						}
					}
					comboBuilder(axisList, 0)
					tasks.sort { it.key }
					parallel tasks
				}
			}
		}
	}
	post {
		always {
			node('master') {
				sh """test -x /usr/local/bin/announce-build-result || exit
				/usr/local/bin/announce-build-result finished with status ${currentBuild.currentResult}
				"""
			}
		}
	}
}
