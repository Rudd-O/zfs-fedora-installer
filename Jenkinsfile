// https://github.com/Rudd-O/shared-jenkins-libraries
@Library('shared-jenkins-libraries@master') _

def buildCmdline(args, short_circuit, break_before) {
	def pname = args["pname"]
	def myBuildFrom = args["buildfrom"]
	def mySourceBranch = args["sourcebranch"]
	def mySeparateBoot = args["separateboot"]
	def myRelease = args["release"]
	def myLuks = args["luks"]

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
	p_short_circuit = short_circuit != null ? "--short-circuit=${short_circuit}" : ""
	p_break_before = break_before != null ? "--break-before=${break_before}" : ""

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
			${p_short_circuit} \\
			${p_break_before} \\
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
		string defaultValue: 'no', description: '', name: 'SEPARATE_BOOT', trim: true
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
		stage('Build ZFS on root images') {
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
								if (!sh(script: "lsmod", label: "Test ZFS is running", returnStdout: true).contains("zfs")) {
									timeout(time: 20, unit: 'MINUTES') {
										sh(
											script: 'if test -f /usr/sbin/setenforce ; then sudo setenforce 0 || exit $? ; fi',
											label: "Turn off SELinux"
										)
										sh(
											script: '''
												deps="rsync rpm-build e2fsprogs dosfstools cryptsetup qemu gdisk python3"
												rpm -q $deps || sudo dnf install -qy $deps
											''',
											label: "Install dependencies"
										)
										sh(
											script: '''
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
											''',
											label: "Install ZFS"
										)
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
											stage("Install OS") {
												timeout(10) {
													sh(
														script: buildCmdline(
															[release: it[0], buildfrom: it[1], luks: it[2], separateboot: it[3], sourcebranch: env.SOURCE_BRANCH != "" ? env.SOURCE_BRANCH : "", pname: "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${it[0]}_${it[1]}_${it[2]}_${it[3]}"],
															null, "deploy_zfs"
														),
														label: "Command run"
													)
												}
											}
											stage("Deploy ZFS") {
												timeout(15) {
													sh(
														script: buildCmdline(
															[release: it[0], buildfrom: it[1], luks: it[2], separateboot: it[3], sourcebranch: env.SOURCE_BRANCH != "" ? env.SOURCE_BRANCH : "", pname: "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${it[0]}_${it[1]}_${it[2]}_${it[3]}"],
															"deploy_zfs", "reload_chroot"
														),
														label: "Command run"
													)
												}
											}
											stage("Prepare OS for bootloader") {
												timeout(5) {
													sh(
														script: buildCmdline([release: it[0], buildfrom: it[1], luks: it[2], separateboot: it[3], sourcebranch: env.SOURCE_BRANCH != "" ? env.SOURCE_BRANCH : "", pname: "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${it[0]}_${it[1]}_${it[2]}_${it[3]}"],
															"reload_chroot", "bootloader_install"
														),
														label: "Command run"
													)
												}
											}
											stage("Install bootloader") {
												timeout(30) {
													sh(
														script: buildCmdline(
															[release: it[0], buildfrom: it[1], luks: it[2], separateboot: it[3], sourcebranch: env.SOURCE_BRANCH != "" ? env.SOURCE_BRANCH : "", pname: "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${it[0]}_${it[1]}_${it[2]}_${it[3]}"],
															"bootloader_install", "boot_to_test_non_hostonly"
														),
														label: "Command run"
													)
												}
											}
											stage("Test generic initrd") {
												timeout(20) {
													sh(
														script: buildCmdline(
															[release: it[0], buildfrom: it[1], luks: it[2], separateboot: it[3], sourcebranch: env.SOURCE_BRANCH != "" ? env.SOURCE_BRANCH : "", pname: "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${it[0]}_${it[1]}_${it[2]}_${it[3]}"],
															"boot_to_test_non_hostonly", "boot_to_test_hostonly"
														),
														label: "Command run"
													)
												}
											}
											stage("Test host-only initrd") {
												timeout(20) {
													sh(
														script: buildCmdline(
															[release: it[0], buildfrom: it[1], luks: it[2], separateboot: it[3], sourcebranch: env.SOURCE_BRANCH != "" ? env.SOURCE_BRANCH : "", pname: "${env.POOL_NAME}_${env.BRANCH_NAME}_${env.BUILD_NUMBER}_${env.GIT_HASH}_${it[0]}_${it[1]}_${it[2]}_${it[3]}"],
															"boot_to_test_hostonly", null
														),
														label: "Command run"
													)
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
