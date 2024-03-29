// https://github.com/Rudd-O/shared-jenkins-libraries
@Library('shared-jenkins-libraries@master') _

def buildCmdline(pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease) {
	if (mySeparateBoot == "yes") {
		mySeparateBoot = "--separate-boot=boot-${pname}.img"
	} else {
		mySeparateBoot = ""
	}
	if (myBuildFrom == "RPMs") {
		myBuildFrom = "--use-prebuilt-rpms=out/fc${myRelease}/"
	} else {
		myBuildFrom = ""
	}
	if (myLuks == "yes") {
		myLuks = "--luks-password=seed"
	} else {
		myLuks = ""
	}
	if (mySourceBranch != "") {
		mySourceBranch = "--use-branch=${env.SOURCE_BRANCH}"
	}

	def program = """
		yumcache="/var/cache/zfs-fedora-installer"
		mntdir="\$PWD/mnt/${pname}"
		mkdir -p "\$mntdir"
		volsize=10000
		cmd=src/install-fedora-on-zfs
		set -x
		set +e
		ret=0
		ls -l
		sudo \\
			python3 -u "\$cmd" \\
			${myBuildFrom} \\
			${mySourceBranch} \\
			${myLuks} \\
			${mySeparateBoot} \\
			--releasever=${myRelease} \\
			--trace-file=/dev/stderr \\
			--workdir="\$mntdir" \\
			--yum-cachedir="\$yumcache" \\
			--host-name="\$HOST_NAME" \\
			--pool-name="${pname}" \\
			--vol-size=\$volsize \\
			--swap-size=256 \\
			--root-password=seed \\
			--chown="\$USER" \\
			--chgrp=`groups | cut -d " " -f 1` \\
			root-${pname}.img >&2
		ret="\$?"
		>&2 echo ==============Diagnostics==================
		>&2 sudo zpool list || true
		>&2 sudo blkid || true
		>&2 sudo lsblk || true
		>&2 sudo losetup -la || true
		>&2 sudo mount || true
		>&2 echo Return value of program: "\$ret"
		>&2 echo =========== End Diagnostics ===============
		if [ "\$ret" == "120" ] ; then ret=0 ; fi
		exit "\$ret"
	""".stripIndent().trim()
	return program
}

pipeline {

	agent none

	options {
		checkoutToSubdirectory 'src'
	}

	parameters {
		string defaultValue: "zfs/${currentBuild.projectName}", description: '', name: 'UPSTREAM_PROJECT', trim: true
		string defaultValue: "", description: 'Use a specific ZFS build number for this test run', name: 'UPSTREAM_PROJECT_BUILD_NUMBER', trim: true
		string defaultValue: 'master', description: '', name: 'SOURCE_BRANCH', trim: true
		string defaultValue: "grub-zfs-fixer/master", description: '', name: 'GRUB_UPSTREAM_PROJECT', trim: true
		string defaultValue: 'no', description: '', name: 'BUILD_FROM_SOURCE', trim: true
		string defaultValue: 'yes', description: '', name: 'BUILD_FROM_RPMS', trim: true
		string defaultValue: 'seed', description: '', name: 'POOL_NAME', trim: true
		string defaultValue: 'seed.dragonfear', description: '', name: 'HOST_NAME', trim: true
		string defaultValue: 'yes', description: '', name: 'SEPARATE_BOOT', trim: true
		// Having trouble with LUKS being yes on Fedora 25.
		string defaultValue: 'no', description: '', name: 'LUKS', trim: true
		string defaultValue: '', description: "Which Fedora releases to build for (empty means the job's default).", name: 'RELEASE', trim: true
	}

	stages {
		stage('Preparation') {
			agent { label 'master' }
			steps {
				announceBeginning()
				script{
					if (params.RELEASE == '') {
						env.RELEASE = funcs.loadParameter('RELEASE', '30')
					} else {
						env.RELEASE = params.RELEASE
					}
				}
				script {
					env.GIT_HASH = sh (
						script: "cd src && git rev-parse --short HEAD",
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
					env.GRUB_UPSTREAM_PROJECT = params.GRUB_UPSTREAM_PROJECT
					if (funcs.isUpstreamCause(currentBuild)) {
						def upstreamProject = funcs.getUpstreamProject(currentBuild)
						env.UPSTREAM_PROJECT = upstreamProject
						env.SOURCE_BRANCH = ""
						env.BUILD_FROM_SOURCE = "no"
						env.BUILD_FROM_RPMS = "yes"
					} else {
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
					env.BUILD_TRIGGER = funcs.describeCause(currentBuild)
					currentBuild.description = "Test of ${env.BUILD_FROM} from source branch ${env.SOURCE_BRANCH} and RPMs from ${env.UPSTREAM_PROJECT}.  ${env.BUILD_TRIGGER}."
				}
			}
		}
		stage('Copy from master') {
			agent { label 'master' }
			when { allOf { not { equals expected: 'NOT_BUILT', actual: currentBuild.result }; equals expected: "", actual: "" } }
			steps {
				dir("out") {
					deleteDir()
				}
				script {
					if (params.UPSTREAM_PROJECT_BUILD_NUMBER == '') {
						copyArtifacts(
							projectName: env.UPSTREAM_PROJECT,
							fingerprintArtifacts: true,
							selector: upstream(fallbackToLastSuccessful: true)
						)
					} else {
						copyArtifacts(
							projectName: env.UPSTREAM_PROJECT,
							fingerprintArtifacts: true,
							selector: specific(params.UPSTREAM_PROJECT_BUILD_NUMBER)
						)
					}
				}
				copyArtifacts(
					projectName: env.GRUB_UPSTREAM_PROJECT,
					fingerprintArtifacts: true,
					selector: upstream(fallbackToLastSuccessful: true)
				)
				sh '{ set +x ; } >/dev/null 2>&1 ; find out/*/*.rpm -type f | sort | grep -v debuginfo | grep -v debugsource | grep -v python | xargs sha256sum > rpmsums'
				stash includes: 'out/*/*.rpm', name: 'rpms', excludes: '**/*debuginfo*,**/*debugsource*,**/*python*'
				stash includes: 'rpmsums', name: 'rpmsums'
				stash includes: 'src/**', name: 'zfs-fedora-installer'
				script {
					env.DETECTED_RELEASES = sh (
						script: "cd out && ls -1 */zfs-dracut*noarch.rpm | sed 's|/.*||' | sed 's|fc||'",
						returnStdout: true
					).trim().replace("\n", ' ')
					println "The detected releases are ${env.DETECTED_RELEASES}"
					if (params.RELEASE == '') {
						println "Overriding releases ${env.RELEASE} with detected releases ${env.DETECTED_RELEASES}"
						env.RELEASE = env.DETECTED_RELEASES
					}
				}
			}
		}
		stage('Parallelize') {
			agent { label 'fedorazfs' }
			options { skipDefaultCheckout() }
			when { not { equals expected: 'NOT_BUILT', actual: currentBuild.result } }
			steps {
				script {
					stage("Check agent") {
						sh(
							script: """#!/bin/sh
									/bin/true
									""",
							label: "Agent is OK"
						)
					}
					stage("Unstash RPMs") {
						script {
							timeout(time: 10, unit: 'MINUTES') {
								sh '{ set +x ; } >/dev/null 2>&1 ; find out/*/*.rpm -type f | sort | grep -v debuginfo | grep -v debugsource | grep -v python | xargs sha256sum > local-rpmsums'
								unstash "rpmsums"
								def needsunstash = sh (
									script: '''
									set +e ; set -x
									output=$(diff -Naur local-rpmsums rpmsums 2>&1)
									if [ "$?" = "0" ]
									then
										echo MATCH
									else
										echo "$output" >&2
									fi
									''',
									returnStdout: true
								).trim()
								if (needsunstash != "MATCH") {
									println "Need to unstash RPMs from master"
									dir("out") {
										deleteDir()
									}
									unstash "rpms"
								}
							}
						}
					}
					stage("Unstash zfs-fedora-installer") {
						unstash "zfs-fedora-installer"
					}
					stage("Activate ZFS") {
						script {
							lock("activatezfs") {
								if (!sh(script: "lsmod", returnStdout: true).contains("zfs")) {
									timeout(time: 20, unit: 'MINUTES') {
										sh 'if test -f /usr/sbin/setenforce ; then sudo setenforce 0 || exit $? ; fi'
										def program = '''
											deps="rsync rpm-build e2fsprogs dosfstools cryptsetup qemu gdisk python3"
											rpm -q \$deps || sudo dnf install -qy \$deps
										'''.stripIndent().trim()
										sh program
										sh '''
											sudo modprobe zfs || {
												eval $(cat /etc/os-release)
												if test -d out/fc$VERSION_ID/ ; then
													sudo src/deploy-zfs --use-prebuilt-rpms out/fc$VERSION_ID/
												else
													sudo src/deploy-zfs
												fi
												sudo modprobe zfs
												sudo service systemd-udevd restart
											}
										'''
									}
								}
							}
						}
					}
					stage("Test") {
						script {
							def axisList = [
								env.RELEASE.split(' '),
								env.BUILD_FROM.split(' '),
								params.LUKS.split(' '),
								params.SEPARATE_BOOT.split(' '),
							]
							def parallelized = funcs.combo(
							    {
							        return {
										stage("${it[0]} ${it[1]} ${it[2]} ${it[3]}") {
											script {
												println "Stage ${it[0]} ${it[1]} ${it[2]} ${it[3]}"
												def myRelease = it[0]
												def myBuildFrom = it[1]
												def myLuks = it[2]
												def mySeparateBoot = it[3]
												def pname = "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${myRelease}_${myBuildFrom}_${myLuks}_${mySeparateBoot}"
												def mySourceBranch = ""
												if (env.SOURCE_BRANCH != "") {
													mySourceBranch = env.SOURCE_BRANCH
												}
												script {
													timeout(60) {
														def program = buildCmdline(pname, myBuildFrom, mySourceBranch, myLuks, mySeparateBoot, myRelease)
														def desc = "============= REPORT ==============\nPool name: ${pname}\nBranch name: ${env.BRANCH_NAME}\nGit hash: ${env.GIT_HASH}\nRelease: ${myRelease}\nBuild from: ${myBuildFrom}\nLUKS: ${myLuks}\nSeparate boot: ${mySeparateBoot}\nSource branch: ${env.SOURCE_BRANCH}\n============= END REPORT =============="
														println "${desc}\n\n" + "Program that will be executed:\n${program}"
														sh(
															script: program,
															label: "Command run"
														)
													}
												}
											}
										}
							        }
								},
								axisList
							)
							parallelized.failFast = true
							parallel parallelized
						}
					}
				}
			}
		}
	}
	post {
		always {
			node('master') {
				announceEnd(currentBuild.currentResult)
			}
		}
	}
}
